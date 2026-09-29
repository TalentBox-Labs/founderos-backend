"""Remote publication truth for Founder OS content jobs.

Adapter success, HTTP status, local or static file writes, rendering flags,
and tracker or frontmatter tokens do not prove a remote CMS write.

PUBLISHED requires an explicit provider acknowledgement plus a remote object
id, an external HTTP write, and rendering_performed true.

VERIFIED is a later read-back. This module never promotes a publish result
straight to verified.
"""

from __future__ import annotations

from typing import Any

TRUTH_UNPROVEN = "unproven"
TRUTH_REMOTE_WRITE_CONFIRMED = "remote_write_confirmed"
TRUTH_VERIFICATION_PENDING = "verification_pending"
TRUTH_VERIFIED = "verified"
TRUTH_FAILED = "failed"
TRUTH_UNKNOWN_REMOTE = "unknown_remote"

PUBLICATION_TRUTHS = frozenset(
    {
        TRUTH_UNPROVEN,
        TRUTH_REMOTE_WRITE_CONFIRMED,
        TRUTH_VERIFICATION_PENDING,
        TRUTH_VERIFIED,
        TRUTH_FAILED,
        TRUTH_UNKNOWN_REMOTE,
    }
)

_AMBIGUOUS_OUTCOMES = frozenset(
    {"unknown", "timeout", "ambiguous", "unknown_remote"}
)
_FAILED_STATUSES = frozenset({"FAILED", "NOT_IMPLEMENTED", "ERROR"})


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def classify_adapter_result(result: dict[str, Any] | None) -> str:
    """Map one adapter payload to publication truth. Does not invent a receipt."""
    if not isinstance(result, dict):
        return TRUTH_FAILED

    outcome = _text(result.get("remote_outcome")).lower()
    dispatched = result.get("remote_request_dispatched") is True
    acknowledged = result.get("remote_write_acknowledged") is True
    remote_id = _text(result.get("remote_object_id"))
    external_http = result.get("external_http") is True
    if result.get("external_http") is None and result.get("external_api_called") is True:
        external_http = True
    rendering_performed = result.get("rendering_performed") is True

    if outcome in _AMBIGUOUS_OUTCOMES or (dispatched and not acknowledged):
        return TRUTH_UNKNOWN_REMOTE
    if acknowledged and result.get("ok") is False:
        return TRUTH_UNKNOWN_REMOTE

    if acknowledged and remote_id and external_http and rendering_performed:
        return TRUTH_REMOTE_WRITE_CONFIRMED

    status = _text(result.get("status")).upper()
    if result.get("ok") is False or status in _FAILED_STATUSES:
        return TRUTH_FAILED
    return TRUTH_UNPROVEN


def local_claim_publication_truth(claim: str | None) -> str:
    """Tracker status and frontmatter tokens are never remote proof."""
    return TRUTH_UNPROVEN


def remote_object_id_from_provider_body(result: Any) -> str:
    """Read an identifier the provider returned. HTTP success alone is ignored."""
    if not isinstance(result, dict):
        return ""
    direct = _text(result.get("id") or result.get("remote_object_id"))
    if direct:
        return direct
    post = result.get("post")
    if isinstance(post, dict):
        nested = _text(post.get("id") or post.get("remote_object_id"))
        if nested:
            return nested
    return ""


def apply_provider_body_to_channel_status(
    status: dict[str, Any],
    channel: str,
    result: Any,
) -> str:
    """Record channel publication truth from a provider body. No synthetic ids."""
    remote_id = remote_object_id_from_provider_body(result)
    if not remote_id:
        status["publication_truth"] = TRUTH_UNPROVEN
        status["verification_status"] = TRUTH_UNPROVEN
        return TRUTH_UNPROVEN
    published = status.setdefault("published", {})
    if isinstance(published, dict):
        published[channel] = True
    status["publication_truth"] = TRUTH_REMOTE_WRITE_CONFIRMED
    status["verification_status"] = TRUTH_VERIFICATION_PENDING
    ids = status.setdefault("remote_object_ids", {})
    if isinstance(ids, dict):
        ids[channel] = remote_id
    return TRUTH_REMOTE_WRITE_CONFIRMED


def mark_channel_remote_unknown(status: dict[str, Any], channel: str) -> None:
    status["publication_truth"] = TRUTH_UNKNOWN_REMOTE
    status["verification_status"] = TRUTH_UNKNOWN_REMOTE
    outstanding = status.setdefault("remote_write_outstanding", {})
    if isinstance(outstanding, dict):
        outstanding[channel] = True


def channel_rewrite_blocked(status: dict[str, Any], channel: str) -> bool:
    outstanding = status.get("remote_write_outstanding") or {}
    if not isinstance(outstanding, dict):
        return False
    return outstanding.get(channel) is True


def verification_from_readback(evidence: dict[str, Any] | None) -> str:
    """Later read-back only. A publish acknowledgement stays verification_pending."""
    if not isinstance(evidence, dict):
        return TRUTH_VERIFICATION_PENDING
    if evidence.get("readback_performed") is not True:
        return TRUTH_VERIFICATION_PENDING
    if not _text(evidence.get("remote_object_id")) or not _text(evidence.get("content_hash")):
        return TRUTH_VERIFICATION_PENDING
    if evidence.get("readback_matches") is True:
        return TRUTH_VERIFIED
    return TRUTH_FAILED
