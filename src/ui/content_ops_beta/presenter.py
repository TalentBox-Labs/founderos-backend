"""Map a Content Ops read envelope into a view model.

Publication success is allowed only when publication_truth is PUBLISHED and
verification_status is VERIFIED. HTTP status, a URL, and allowed_actions do
not create that state. Actions are rendered only from allowed_actions.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from src.ui.content_ops_beta.contract import (
    ACTION_CATALOG,
    DECISION_ACTION_IDS,
    PILL_BY_TONE,
    PIPELINE_STEPS,
    PROJECTION_FIELDS,
    STAGE_TO_STEP,
    TARGET_READ_CONTRACT,
)

_ERROR_TITLES = {
    "UNAUTHORIZED": "Sign-in required",
    "FORBIDDEN": "Read forbidden",
    "ACTIVE_INTENT": "Active intent conflict",
    "POLICY_FAILURE": "Policy failure",
    "UNKNOWN_SCENARIO": "Unknown mock scenario",
}


def present_content_ops_read(raw: dict[str, Any]) -> dict[str, Any]:
    source = str(raw.get("source") or "unknown")
    live = bool(raw.get("live")) and source != "mock"
    state = str(raw.get("state") or "error")
    base = {
        "source": source,
        "live": live,
        "contract": str(raw.get("contract") or TARGET_READ_CONTRACT),
        "scenario": str(raw.get("scenario") or ""),
        "http_status": int(raw.get("http_status") or 0),
        "read_state": state if state in {"ready", "empty", "loading", "error"} else "error",
        "scenarios": list(raw.get("available_scenarios") or []),
        "mock_banner": source == "mock" or not live,
    }
    if base["read_state"] == "loading":
        return base
    if base["read_state"] == "error" or not raw.get("ok"):
        base["read_state"] = "error"
        base["error"] = _present_error(raw.get("error"), base["http_status"])
        return base
    projection = raw.get("projection")
    if not isinstance(projection, dict):
        base["read_state"] = "error"
        base["error"] = _present_error(
            {"code": "MALFORMED_READ", "message": "The read did not include a week projection."},
            base["http_status"],
        )
        return base
    if base["read_state"] == "empty" or _is_empty(projection):
        base["read_state"] = "empty"
        return base
    base["read_state"] = "ready"
    base.update(_present_ready(projection, raw))
    return base


def _is_empty(projection: dict[str, Any]) -> bool:
    return not projection.get("week_id")


def _present_error(error: Any, http_status: int) -> dict[str, Any]:
    payload = error if isinstance(error, dict) else {}
    code = str(payload.get("code") or "READ_FAILED")
    message = str(payload.get("message") or "The Content Ops read failed. No week was loaded.")
    presented = {
        "code": code,
        "title": _ERROR_TITLES.get(code, "Read failed"),
        "message": message,
        "http_status": http_status,
        "week_id": payload.get("week_id"),
        "policy": payload.get("policy"),
    }
    return presented


def _present_ready(projection: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    allowed = _allowed_actions(projection.get("allowed_actions"))
    failure = _failure(projection.get("failure"))
    publication = _publication_view(projection, allowed)
    stage = _text(projection.get("stage")) or "UNKNOWN"
    qa_status = _upper(projection.get("qa_status")) or "UNKNOWN"
    risk_class = _upper(projection.get("risk_class")) or "UNKNOWN"
    approval_required = projection.get("approval_required") is True
    decision_ids = [action_id for action_id in allowed if action_id in DECISION_ACTION_IDS]
    return {
        "week_id": _text(projection.get("week_id")),
        "stage": stage,
        "stage_label": stage.replace("_", " "),
        "next_action": _text(projection.get("next_action")) or "No next action was reported.",
        "attention": _attention(projection, failure),
        "pipeline": _pipeline(stage, publication["tone"]),
        "qa": {
            "status": qa_status,
            "tone": _qa_tone(qa_status),
            "pill": PILL_BY_TONE[_qa_tone(qa_status)],
            "risk_class": risk_class,
            "risk_tone": _risk_tone(risk_class),
            "risk_pill": PILL_BY_TONE[_risk_tone(risk_class)],
        },
        "approval_required": approval_required,
        "decision_waiting": approval_required or bool(decision_ids),
        "decision_actions": [_action(action_id) for action_id in decision_ids],
        "decision_blocked": approval_required and not decision_ids,
        "week_actions": [
            _action(action_id)
            for action_id in allowed
            if ACTION_CATALOG[action_id]["group"] == "week"
        ],
        "publication": publication,
        "run": {
            "last_run_at": _text(projection.get("last_run_at")) or "—",
            "next_schedule_at": _text(projection.get("next_schedule_at")) or "—",
            "failure": failure,
        },
        "artifacts": _artifacts(projection.get("artifact_refs")),
        "show_artifacts": "open_artifact" in allowed,
        "show_audit": "view_audit" in allowed,
        "unrecognized_actions": _unrecognized(projection.get("allowed_actions")),
        "audit_json": json.dumps(_audit_payload(projection, raw), indent=2, sort_keys=True),
        "has_actions": bool(allowed),
    }


def _attention(projection: dict[str, Any], failure: dict[str, str] | None) -> str:
    if failure and failure.get("message"):
        return failure["message"]
    reported = _text(projection.get("next_action"))
    if reported:
        return reported
    return "No next action was reported."


def _publication_view(projection: dict[str, Any], allowed: list[str]) -> dict[str, Any]:
    truth = _upper(projection.get("publication_truth")) or "UNKNOWN"
    verification = _upper(projection.get("verification_status")) or "UNKNOWN"
    status = _upper(projection.get("publication_status")) if projection.get("publication_status") else None
    url = _safe_http_url(projection.get("published_url"))
    proven = truth == "PUBLISHED" and verification == "VERIFIED"
    headline, tone = _publication_headline(truth, verification, proven)
    action_listed = "open_published_url" in allowed
    show_success = proven and action_listed and url is not None
    return {
        "status": status or "—",
        "truth": truth,
        "verification": verification,
        "headline": headline,
        "tone": tone,
        "pill": PILL_BY_TONE[tone],
        "url": url,
        "url_on_record": url is not None,
        "url_withheld": projection.get("published_url") not in (None, "") and url is None,
        "show_success_link": show_success,
        "withhold_success": action_listed and not show_success,
        "unverified_label": "PUBLICATION UNVERIFIED" if truth == "UNVERIFIED" and not proven else None,
    }


def _publication_headline(truth: str, verification: str, proven: bool) -> tuple[str, str]:
    if proven:
        return "PUBLISHED / VERIFIED", "verified"
    if verification == "PENDING":
        return "VERIFICATION PENDING", "attention"
    if truth == "UNPROVEN":
        return "PUBLICATION UNPROVEN", "attention"
    if truth == "UNVERIFIED" or verification == "UNVERIFIED":
        return "PUBLICATION UNVERIFIED", "attention"
    if truth == "NOT_PUBLISHED":
        return "NOT PUBLISHED", "neutral"
    return "PUBLICATION UNKNOWN", "unknown"


def _pipeline(stage: str, publication_tone: str) -> list[dict[str, str]]:
    current = STAGE_TO_STEP.get(stage.strip().lower())
    steps: list[dict[str, str]] = []
    for step_id, label in PIPELINE_STEPS:
        if stage.strip().lower() == "failed" or current is None:
            state = "idle"
            state_label = "Not current"
        elif step_id == current and publication_tone == "verified" and step_id == "verification":
            state = "verified"
            state_label = "Verified"
        elif step_id == current:
            state = "current"
            state_label = "Current"
        else:
            state = "idle"
            state_label = "Not current"
        steps.append(
            {
                "id": step_id,
                "label": label,
                "state": state,
                "state_label": state_label,
            }
        )
    return steps


def _allowed_actions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        action_id = str(item)
        if action_id in ACTION_CATALOG and action_id not in seen:
            seen.append(action_id)
    return seen


def _unrecognized(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item) not in ACTION_CATALOG]


def _action(action_id: str) -> dict[str, str]:
    meta = ACTION_CATALOG[action_id]
    emphasis = "primary" if action_id in {"run_now", "approve", "retry_failed_stage"} else "ghost"
    if action_id == "reject":
        emphasis = "danger"
    return {
        "id": action_id,
        "label": meta["label"],
        "emphasis": emphasis,
    }


def _artifacts(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        ref_id = _text(item.get("id"))
        label = _text(item.get("label")) or ref_id
        if not ref_id or not label:
            continue
        refs.append({"id": ref_id, "label": label})
    return refs


def _failure(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    message = _text(value.get("message"))
    if not message:
        return None
    return {
        "stage": _text(value.get("stage")) or "—",
        "code": _text(value.get("code")) or "FAILED",
        "message": message,
    }


def _audit_payload(projection: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    projected = {field: projection.get(field) for field in PROJECTION_FIELDS}
    return {
        "source": raw.get("source"),
        "live": False if raw.get("source") == "mock" else bool(raw.get("live")),
        "contract": raw.get("contract") or TARGET_READ_CONTRACT,
        "http_status": raw.get("http_status"),
        "projection": projected,
        "note": "Audit of this read envelope. Not a live publication record.",
    }


def _qa_tone(status: str) -> str:
    if status == "PASS":
        return "verified"
    if status in {"FAIL", "FAILED"}:
        return "risk"
    if status in {"NOT_RUN", "PENDING"}:
        return "attention"
    return "unknown"


def _risk_tone(risk_class: str) -> str:
    if risk_class == "HIGH":
        return "risk"
    if risk_class == "MEDIUM":
        return "attention"
    if risk_class == "LOW":
        return "neutral"
    return "unknown"


def _safe_http_url(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return text
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _upper(value: Any) -> str | None:
    text = _text(value)
    return text.upper() if text else None
