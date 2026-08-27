"""Gmail inbound-email sync — real OAuth2 + Gmail REST API, read-only.

No scraping, no IMAP guesswork: this is Google's own OAuth2 authorization-
code flow (offline access for a refresh token) against the official Gmail
API (`gmail.readonly` scope). Matched messages are logged to the sending
contact's timeline; unmatched senders are skipped rather than dumped in as
noise — this is about aligning mail to an existing account, not an inbox
importer.

Tenant contract (P0):
  organization_id → tenant-scoped vault credentials → mailbox →
  Contact(email AND organization_id) → tenant-scoped activity/dedupe.
Missing organization_id fails closed. No global credential fallback.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_TIMEOUT = 15
_MAX_MESSAGES_PER_SYNC = 25

CONNECTOR_NAME = "gmail"


def _vault_config(organization_id: str) -> dict[str, Any] | None:
    """Load Gmail credentials for one organization only — never NULL-org global."""
    from revenue_os.services.credentials_vault import load_credentials

    return load_credentials(
        CONNECTOR_NAME,
        organization_id=organization_id,
        allow_global_fallback=False,
    )


def build_authorize_url(client_id: str, redirect_uri: str, state: str = "") -> str:
    """Build the Google OAuth2 consent URL. offline+consent so we get a refresh_token."""
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange an authorization code for access + refresh tokens. Never raises."""
    import requests

    try:
        resp = requests.post(
            _TOKEN_URL,
            data={
                "code": code, "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return {"ok": True, **resp.json()}
    except Exception as e:
        logger.warning(f"Gmail token exchange failed: {e}")
        return {"ok": False, "error": str(e)}


def _refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    import requests

    try:
        resp = requests.post(
            _TOKEN_URL,
            data={
                "client_id": client_id, "client_secret": client_secret,
                "refresh_token": refresh_token, "grant_type": "refresh_token",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return {"ok": True, **resp.json()}
    except Exception as e:
        logger.warning(f"Gmail token refresh failed: {e}")
        return {"ok": False, "error": str(e)}


_ADDR_RE = re.compile(r"<([^>]+)>")


def _extract_email(from_header: str) -> str:
    """Pull a bare email address out of a From header like 'Name <a@b.com>'."""
    match = _ADDR_RE.search(from_header or "")
    return (match.group(1) if match else (from_header or "")).strip().lower()


def _list_message_ids(access_token: str, max_results: int) -> list[str]:
    import requests

    resp = requests.get(
        f"{_API_BASE}/messages",
        params={"maxResults": max_results, "labelIds": "INBOX"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("messages", [])]


def _get_message(access_token: str, message_id: str) -> dict[str, Any]:
    import requests

    resp = requests.get(
        f"{_API_BASE}/messages/{message_id}",
        params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _resolve_sync_organization_id(
    *,
    organization_id: str | None,
    organization_ids: list[str] | None,
) -> str | None:
    """Require exactly one tenant. Multi-org batch mapping is forbidden."""
    if organization_id is not None and str(organization_id).strip():
        if organization_ids and len(organization_ids) > 1:
            return None
        return str(organization_id).strip()
    if organization_ids is None:
        return None
    cleaned = [str(o).strip() for o in organization_ids if o is not None and str(o).strip()]
    if len(cleaned) == 1:
        return cleaned[0]
    return None


def sync_inbox(
    *,
    organization_id: str | None = None,
    organization_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Pull recent inbox messages and log matched-contact ones to their timeline.

    Requires an explicit organization_id (single tenant). organization_ids with
    exactly one entry is accepted for call-site compatibility; empty/multi fails closed.
    """
    from revenue_os.services.acp1_autonomous_boundary import (
        BLOCKED_MISSING_TENANT,
        assert_contact_org,
        log_autonomous_blocked,
        org_uuid_or_none,
    )

    resolved_org = _resolve_sync_organization_id(
        organization_id=organization_id,
        organization_ids=organization_ids,
    )
    if not resolved_org:
        return log_autonomous_blocked(
            actor="heartbeat",
            action_type="gmail_sync_blocked",
            reason=BLOCKED_MISSING_TENANT,
            detail={
                "note": "Gmail sync requires explicit single-organization scope",
                "organization_id": organization_id,
                "organization_ids": organization_ids,
            },
        )

    org_uuid = org_uuid_or_none(resolved_org)
    if org_uuid is None:
        return log_autonomous_blocked(
            actor="heartbeat",
            action_type="gmail_sync_blocked",
            reason=BLOCKED_MISSING_TENANT,
            detail={"note": "No valid organization UUID for Gmail sync"},
        )

    config = _vault_config(resolved_org)
    if not config or not config.get("refresh_token"):
        return {
            "ok": False,
            "reason": "Gmail is not connected for this organization — connect it on the Integrations page.",
            "organization_id": resolved_org,
        }

    refreshed = _refresh_access_token(
        config["client_id"], config["client_secret"], config["refresh_token"]
    )
    if not refreshed.get("ok"):
        return {
            "ok": False,
            "reason": f"Could not refresh Gmail access token: {refreshed.get('error')}",
            "organization_id": resolved_org,
        }
    access_token = refreshed["access_token"]

    try:
        message_ids = _list_message_ids(access_token, _MAX_MESSAGES_PER_SYNC)
    except Exception as e:
        logger.warning(f"Gmail message list failed: {e}")
        return {"ok": False, "reason": str(e), "organization_id": resolved_org}

    from revenue_os.database import SessionLocal
    from revenue_os.models.activity import Activity, ActivityType, EmailActivity
    from revenue_os.models.contact import Contact
    from revenue_os.services.activity_log import log_agent_action

    db = SessionLocal()
    checked = matched = created = 0
    try:
        # Dedupe only against EmailActivity rows already attributed to this org's contacts.
        already_synced = {
            row.message_id
            for row in (
                db.query(EmailActivity.message_id)
                .join(Activity, EmailActivity.activity_id == Activity.id)
                .join(Contact, Activity.contact_id == Contact.id)
                .filter(
                    EmailActivity.message_id.in_(message_ids),
                    Contact.organization_id == org_uuid,
                )
                .all()
            )
        }
        contacts_by_email = {
            c.email.lower(): c
            for c in db.query(Contact)
            .filter(
                Contact.email.isnot(None),
                Contact.organization_id == org_uuid,
            )
            .all()
            if c.email
        }

        for message_id in message_ids:
            checked += 1
            if message_id in already_synced:
                continue
            try:
                message = _get_message(access_token, message_id)
            except Exception as e:
                logger.warning(f"Gmail message fetch failed ({message_id}): {e}")
                continue

            headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
            sender = _extract_email(headers.get("From", ""))
            contact = contacts_by_email.get(sender)
            if contact is None:
                continue  # not a known in-scope contact — skip rather than dump inbox noise

            org_id = str(contact.organization_id) if contact.organization_id else None
            if org_id is None or org_id != resolved_org:
                continue
            if not assert_contact_org(contact, resolved_org):
                continue

            matched += 1
            activity = Activity(
                contact_id=contact.id, activity_type=ActivityType.EMAIL, direction="inbound",
                subject=headers.get("Subject", "")[:500], body=message.get("snippet", ""),
                status="completed",
            )
            db.add(activity)
            db.flush()
            db.add(EmailActivity(
                activity_id=activity.id, message_id=message_id,
                from_address=sender,
            ))
            contact.last_contacted_at = datetime.now(timezone.utc)
            db.add(contact)
            created += 1
            log_agent_action(
                actor="heartbeat",
                action_type="gmail_inbound_matched",
                target_type="contact",
                target_id=str(contact.id),
                organization_id=resolved_org,
                detail={"message_id": message_id},
            )

        db.commit()
    finally:
        db.close()

    return {
        "ok": True,
        "checked": checked,
        "matched": matched,
        "created": created,
        "organization_id": resolved_org,
        "organizations": [resolved_org],
    }
