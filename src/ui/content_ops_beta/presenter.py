"""Map a Content Ops read envelope into a view model.

Publication success is allowed only when publication_truth and
verification_status are both the backend token ``verified`` and the server
listed ``open_published_url``. A URL, HTTP 200, or another token does not
create that state. Actions are rendered only from allowed_actions.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from src.tools.content_ops_readiness import autonomous_publication_ready
from src.ui.content_ops_beta.contract import (
    ACTION_CATALOG,
    DECISION_ACTION_IDS,
    PILL_BY_TONE,
    PIPELINE_STEPS,
    PROJECTION_FIELDS,
    STAGE_TO_STEP,
    TARGET_READ_CONTRACT,
    TRUTH_PRESENTATION,
)

_ERROR_TITLES = {
    "UNAUTHORIZED": "Sign-in required",
    "FORBIDDEN": "Content Ops access denied",
    "ACTIVE_INTENT": "Active intent conflict",
    "POLICY_FAILURE": "Policy failure",
    "SERVER_ERROR": "Content Ops unavailable",
    "UNKNOWN_SCENARIO": "Unknown fixture scenario",
    "MALFORMED_READ": "Current week read is unusable",
}

_EMPTY_COPY = {
    "no_current_week": (
        "No active week",
        "This read has no active Content Ops week. There is no lifecycle stage, publication result, or verification result to show.",
    ),
    "malformed_current_week": (
        "Current week setting is unusable",
        "The server's current week setting is not a usable week id. No week was invented.",
    ),
    "current_week_not_in_tracker": (
        "Current week is not in the tracker",
        "The configured week is not in the content tracker. No week was invented.",
    ),
}

_MUTATION_ACTIONS = frozenset(
    {"run_now", "approve", "reject", "request_changes", "retry", "cancel"}
)


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
        "week_id": "",
        "publication_job_id": "",
        "readiness_surface": "content_review_internal_beta",
        "autonomous_publication_ready": autonomous_publication_ready(),
        "empty_reason": "",
        "empty_title": "No active week",
        "empty_message": _EMPTY_COPY["no_current_week"][1],
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
        base["http_status"] = base["http_status"] or 500
        base["error"] = _present_error(
            {
                "code": "MALFORMED_READ",
                "message": "The current week read was incomplete. No week was loaded.",
            },
            base["http_status"],
        )
        return base
    if base["read_state"] == "empty" or _is_empty(projection):
        reason = str(raw.get("reason") or "no_current_week")
        title, message = _EMPTY_COPY.get(reason, _EMPTY_COPY["no_current_week"])
        base["read_state"] = "empty"
        base["empty_reason"] = reason
        base["empty_title"] = title
        base["empty_message"] = message
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
    publication = _publication_view(projection, allowed)
    failure = _failure(projection.get("failure"), publication["truth"])
    stage = _text(projection.get("stage")) or "Not reported"
    qa_status = _upper(projection.get("qa_status")) or "Not reported"
    risk_class = _upper(projection.get("risk_class")) or "Not reported"
    approval_required = projection.get("approval_required") is True
    job_id = _text(projection.get("publication_job_id")) or ""
    decision_ids = [action_id for action_id in allowed if action_id in DECISION_ACTION_IDS]
    week_actions = [
        action
        for action_id in allowed
        if ACTION_CATALOG[action_id]["group"] == "week"
        for action in [_action(action_id, job_id)]
        if action is not None
    ]
    return {
        "week_id": _text(projection.get("week_id")),
        "publication_job_id": job_id,
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
        "decision_actions": [
            action
            for action_id in decision_ids
            for action in [_action(action_id, job_id)]
            if action is not None
        ],
        "decision_blocked": approval_required and not decision_ids,
        "week_actions": week_actions,
        "publication": publication,
        "run": {
            "last_run_at": _text(projection.get("last_run_at")) or "No run recorded",
            "next_schedule_at": _text(projection.get("next_schedule_at")) or "No upcoming schedule",
            "failure": failure,
        },
        "artifacts": _artifacts(projection.get("artifact_refs"), _text(projection.get("week_id")) or ""),
        "show_artifacts": "open_artifact" in allowed,
        "show_audit": "view_audit" in allowed,
        "unrecognized_actions": _unrecognized(projection.get("allowed_actions")),
        "audit_json": json.dumps(_audit_payload(projection, raw), indent=2, sort_keys=True),
        "has_actions": bool(
            week_actions
            or decision_ids
            or "open_artifact" in allowed
            or "view_audit" in allowed
            or publication["show_success_link"]
        ),
    }


def _attention(projection: dict[str, Any], failure: dict[str, str] | None) -> str:
    if failure and failure.get("message"):
        return failure["message"]
    reported = _text(projection.get("next_action"))
    if reported:
        return reported
    return "No next action was reported."


def _publication_view(projection: dict[str, Any], allowed: list[str]) -> dict[str, Any]:
    truth = _token(projection.get("publication_truth")) or "unproven"
    verification = _token(projection.get("verification_status")) or "unproven"
    status = _text(projection.get("publication_status")) or "—"
    url = _safe_http_url(projection.get("published_url"))
    proven = truth == "verified" and verification == "verified"
    headline, tone = _publication_headline(truth, verification)
    action_listed = "open_published_url" in allowed
    show_success = proven and action_listed and url is not None
    return {
        "status": status,
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
        "unverified_label": None,
    }


def _publication_headline(truth: str, verification: str) -> tuple[str, str]:
    if truth == "unknown_remote":
        return TRUTH_PRESENTATION["unknown_remote"]
    if truth == "failed":
        return TRUTH_PRESENTATION["failed"]
    if truth == "verified" and verification == "verified":
        return TRUTH_PRESENTATION["verified"]
    if verification == "verification_pending" and truth in {
        "remote_write_confirmed",
        "verification_pending",
        "unproven",
    }:
        return TRUTH_PRESENTATION["verification_pending"]
    if truth in TRUTH_PRESENTATION:
        return TRUTH_PRESENTATION[truth]
    return "Publication status unrecognized", "unknown"


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


def _action(action_id: str, job_id: str) -> dict[str, str] | None:
    if action_id in {"retry", "cancel"} and not job_id:
        return None
    meta = ACTION_CATALOG[action_id]
    emphasis = "primary" if action_id in {"run_now", "approve"} else "ghost"
    if action_id == "reject":
        emphasis = "danger"
    return {
        "id": action_id,
        "label": meta["label"],
        "emphasis": emphasis,
        "executes": "true" if action_id in _MUTATION_ACTIONS else "false",
    }


def _artifacts(value: Any, week_id: str) -> list[dict[str, str]]:
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
        refs.append(
            {
                "id": ref_id,
                "label": label,
                "href": _artifact_href(week_id, item.get("path")),
            }
        )
    return refs


def _artifact_href(week_id: str, path: Any) -> str:
    text = _text(path)
    prefix = f"input/{week_id}/"
    if not week_id or not text or not text.startswith(prefix):
        return ""
    name = text[len(prefix) :]
    if not name or "/" in name or "\\" in name or ".." in name:
        return ""
    return f"/weeks/{week_id}/file/{name}"


def _failure(value: Any, truth: str) -> dict[str, str] | None:
    kind = "unknown" if truth == "unknown_remote" else "failed"
    code = "UNKNOWN_REMOTE" if kind == "unknown" else "FAILED"
    if isinstance(value, str):
        message = value.strip()
        if not message:
            return None
        return {"stage": "—", "code": code, "message": message, "kind": kind}
    if not isinstance(value, dict):
        return None
    message = _text(value.get("message"))
    if not message:
        return None
    return {
        "stage": _text(value.get("stage")) or "—",
        "code": _text(value.get("code")) or code,
        "message": message,
        "kind": kind,
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


def _token(value: Any) -> str | None:
    text = _text(value)
    return text.lower() if text else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _upper(value: Any) -> str | None:
    text = _text(value)
    return text.upper() if text else None
