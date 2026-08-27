"""Integration management API endpoints."""

from __future__ import annotations

import logging
import os
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from jose import JWTError, jwt

from revenue_os.config import settings
from revenue_os.database import SessionLocal
from revenue_os.integrations.email import EmailNotifier, ScheduledEmailQueue, EmailTemplate
from revenue_os.integrations.webhooks import WebhookManager, WebhookEventType
from revenue_os.integrations.calendar import GoogleCalendarClient, OutlookCalendarClient, CalendarEvent
from revenue_os.integrations.slack import SlackNotifier

from runner_api_routers.utils import _verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])


# Every integration the platform knows about — the "connector registry".
# Vault-backed connectors are configured (and encrypted) through this API;
# env-backed ones are set at deploy time and only reported as configured/not.
CONNECTOR_CATALOG: list[dict[str, Any]] = [
    {
        "name": "email_smtp", "label": "Email (SMTP)", "category": "email", "source": "vault",
        "fields": [
            {"key": "host", "label": "SMTP Host", "type": "text"},
            {"key": "port", "label": "Port", "type": "number"},
            {"key": "username", "label": "Username", "type": "text"},
            {"key": "password", "label": "Password", "type": "password"},
            {"key": "from_email", "label": "From Address", "type": "text"},
        ],
    },
    {
        "name": "slack", "label": "Slack", "category": "notifications", "source": "vault",
        "fields": [{"key": "webhook_url", "label": "Webhook URL", "type": "password"}],
    },
    {
        "name": "whatsapp", "label": "WhatsApp Business", "category": "messaging", "source": "vault",
        "fields": [
            {"key": "api_key", "label": "API Key", "type": "password"},
            {"key": "phone_number_id", "label": "Phone Number ID", "type": "text"},
            {"key": "business_account_id", "label": "Business Account ID", "type": "text"},
        ],
    },
    {
        "name": "google_calendar", "label": "Google Calendar", "category": "calendar", "source": "vault",
        "fields": [
            {"key": "access_token", "label": "Access Token", "type": "password"},
            {"key": "refresh_token", "label": "Refresh Token", "type": "password"},
            {"key": "calendar_id", "label": "Calendar ID", "type": "text"},
        ],
    },
    {
        "name": "outlook_calendar", "label": "Outlook Calendar", "category": "calendar", "source": "vault",
        "fields": [
            {"key": "tenant_id", "label": "Tenant ID", "type": "text"},
            {"key": "access_token", "label": "Access Token", "type": "password"},
        ],
    },
    {
        "name": "linkedin_enrichment", "label": "LinkedIn Enrichment (Proxycurl)", "category": "enrichment", "source": "vault",
        "fields": [{"key": "api_key", "label": "Proxycurl API Key", "type": "password"}],
    },
    {
        "name": "gmail", "label": "Gmail Sync", "category": "email", "source": "vault", "oauth": True,
        "fields": [
            {"key": "client_id", "label": "OAuth Client ID", "type": "text"},
            {"key": "client_secret", "label": "OAuth Client Secret", "type": "password"},
            {"key": "redirect_uri", "label": "Redirect URI (must match the OAuth client's registered URI)", "type": "text"},
        ],
    },
    {"name": "n8n", "label": "n8n", "category": "automation", "source": "env",
     "env_vars": ["N8N_WEBHOOK_BASE_URL", "N8N_API_KEY"]},
    {"name": "openai", "label": "OpenAI", "category": "ai", "source": "env",
     "env_vars": ["OPENAI_API_KEY"]},
    {"name": "hashnode", "label": "Hashnode", "category": "content", "source": "env",
     "env_vars": ["HASHNODE_ACCESS_TOKEN"]},
    {"name": "linkedin", "label": "LinkedIn", "category": "content", "source": "env",
     "env_vars": ["LINKEDIN_ACCESS_TOKEN"]},
    {"name": "instagram", "label": "Instagram", "category": "content", "source": "env",
     "env_vars": ["INSTAGRAM_ACCESS_TOKEN"]},
    {"name": "youtube", "label": "YouTube", "category": "content", "source": "env",
     "env_vars": ["YOUTUBE_API_KEY"]},
]

_VAULT_CONFIGURE_HANDLERS = {
    "email_smtp": lambda config: EmailNotifier.configure_smtp(config),
    "slack": lambda config: SlackNotifier.set_webhook_url(config.get("webhook_url", "")),
    "google_calendar": lambda config: GoogleCalendarClient.configure(config),
    "outlook_calendar": lambda config: OutlookCalendarClient.configure(
        tenant_id=config.get("tenant_id", ""), access_token=config.get("access_token", ""),
    ),
}


def _configure_whatsapp(config: dict[str, Any]) -> None:
    from revenue_os.integrations.whatsapp import WhatsAppClient

    WhatsAppClient.configure(
        api_key=config.get("api_key", ""),
        phone_number_id=config.get("phone_number_id", ""),
        business_account_id=config.get("business_account_id", ""),
    )


_VAULT_CONFIGURE_HANDLERS["whatsapp"] = _configure_whatsapp


def _integration_org_id() -> str | None:
    from revenue_os.services.integration_tenant_resolution import resolve_integration_org_id

    return resolve_integration_org_id()


@router.get("/connectors", tags=["integrations"])
def list_connectors(_: str | None = Depends(_verify_api_key)) -> dict[str, Any]:
    """Every integration the platform knows about, merged with live status.

    Never returns secret values — only whether each connector is configured.
    """
    from revenue_os.services.credentials_vault import list_configured_connectors

    from revenue_os.services.credentials_vault import load_credentials

    org_id = _integration_org_id()
    vaulted = list_configured_connectors(organization_id=org_id)
    result = []
    for c in CONNECTOR_CATALOG:
        if c["source"] == "vault":
            entry = vaulted.get(c["name"])
            configured = entry is not None
            updated_at = entry["updated_at"] if entry else None
        else:
            configured = bool(c.get("env_vars")) and all(os.environ.get(v) for v in c["env_vars"])
            updated_at = None

        connected = None
        if c.get("oauth"):
            # OAuth connectors have two states: fields saved (configured) vs.
            # consent completed (connected, has a refresh_token). Only decrypt
            # for the presence check — never expose the token itself.
            connected = configured and bool(
                (load_credentials(c["name"], organization_id=org_id) or {}).get("refresh_token")
            )

        result.append({
            "name": c["name"], "label": c["label"], "category": c["category"],
            "source": c["source"], "configured": configured, "updated_at": updated_at,
            "fields": c.get("fields", []), "env_vars": c.get("env_vars", []),
            "oauth": bool(c.get("oauth")), "connected": connected,
        })
    return {"ok": True, "count": len(result), "connectors": result}


@router.post("/connectors/{connector_name}/configure", tags=["integrations"])
def configure_connector(
    connector_name: str,
    config: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Configure any vault-backed connector — encrypted at rest, survives restart."""
    catalog_entry = next((c for c in CONNECTOR_CATALOG if c["name"] == connector_name), None)
    if catalog_entry is None:
        raise HTTPException(status_code=404, detail="Unknown connector")
    if catalog_entry["source"] != "vault":
        raise HTTPException(
            status_code=400,
            detail=f"{catalog_entry['label']} is configured via environment variables "
                   f"({', '.join(catalog_entry.get('env_vars', []))}), not this API.",
        )

    handler = _VAULT_CONFIGURE_HANDLERS.get(connector_name)
    if handler:
        try:
            handler(config)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid configuration: {e}")

    from revenue_os.services.credentials_vault import save_credentials

    org_id = _integration_org_id()
    if connector_name == "gmail" and not org_id:
        raise HTTPException(
            status_code=400,
            detail="Organization context required to configure Gmail.",
        )
    save_credentials(connector_name, catalog_entry["category"], config, organization_id=org_id)
    return {"ok": True, "message": f"{catalog_entry['label']} configured"}


@router.delete("/connectors/{connector_name}", tags=["integrations"])
def remove_connector_credentials(
    connector_name: str,
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Remove a connector's stored credentials."""
    from revenue_os.services.credentials_vault import delete_credentials

    org_id = _integration_org_id()
    deleted = delete_credentials(connector_name, organization_id=org_id)
    return {"ok": deleted}


@router.post("/email/configure", tags=["integrations"])
def configure_email(
    config: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Configure email (SMTP) settings — persisted encrypted, survives restart."""
    logger.info("Configuring email settings")

    try:
        EmailNotifier.configure_smtp(config)
        from revenue_os.services.credentials_vault import save_credentials

        org_id = _integration_org_id()
        save_credentials("email_smtp", "email", config, organization_id=org_id)
        return {"ok": True, "message": "Email configured successfully"}
    except Exception as e:
        logger.error(f"Failed to configure email: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/email/templates", tags=["integrations"])
def register_email_template(
    template: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Register an email template."""
    logger.info(f"Registering email template: {template.get('name')}")

    try:
        email_template = EmailTemplate(
            name=template["name"],
            subject=template["subject"],
            html_body=template["html_body"],
            text_body=template.get("text_body", ""),
            variables=template.get("variables", []),
        )
        EmailNotifier.register_template(email_template)
        return {"ok": True, "message": f"Template '{email_template.name}' registered"}
    except Exception as e:
        logger.error(f"Failed to register email template: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/email/send", tags=["integrations"])
def send_email(
    payload: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Send an email."""
    logger.info(f"Sending email to {payload.get('to_email')}")

    try:
        success = EmailNotifier.send_email(
            to_email=payload["to_email"],
            subject=payload["subject"],
            html_body=payload["html_body"],
            text_body=payload.get("text_body"),
            cc=payload.get("cc"),
            bcc=payload.get("bcc"),
        )
        return {"ok": success, "message": "Email sent" if success else "Failed to send email"}
    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/email/template-send", tags=["integrations"])
def send_template_email(
    payload: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Send email using a template."""
    logger.info(f"Sending template email: {payload.get('template_name')}")

    try:
        success = EmailNotifier.send_template_email(
            template_name=payload["template_name"],
            to_email=payload["to_email"],
            context=payload.get("context", {}),
            cc=payload.get("cc"),
            bcc=payload.get("bcc"),
        )
        return {"ok": success, "message": "Email sent" if success else "Template not found"}
    except Exception as e:
        logger.error(f"Failed to send template email: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/email/schedule", tags=["integrations"])
def schedule_email(
    payload: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Schedule an email for future send."""
    logger.info(f"Scheduling email for {payload.get('to_email')}")

    try:
        from datetime import datetime as dt

        send_at = dt.fromisoformat(payload["send_at"])
        success = ScheduledEmailQueue.schedule_email(
            to_email=payload["to_email"],
            template_name=payload["template_name"],
            context=payload.get("context", {}),
            send_at=send_at,
            cc=payload.get("cc"),
        )
        return {"ok": success, "message": "Email scheduled"}
    except Exception as e:
        logger.error(f"Failed to schedule email: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/email/process-scheduled", tags=["integrations"])
def process_scheduled_emails(
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Process and send all pending scheduled emails."""
    logger.info("Processing scheduled emails")

    try:
        result = ScheduledEmailQueue.process_pending_emails()
        return result
    except Exception as e:
        logger.error(f"Failed to process scheduled emails: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/webhooks/subscribe", tags=["integrations"])
def create_webhook_subscription(
    payload: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Create a webhook subscription."""
    logger.info(f"Creating webhook subscription for {payload.get('url')}")

    try:
        events = [WebhookEventType(e) for e in payload.get("events", [])]
        subscription = WebhookManager.create_subscription(
            url=payload["url"],
            events=events,
            metadata=payload.get("metadata", {}),
        )
        return {
            "ok": True,
            "subscription": subscription.to_dict(),
        }
    except Exception as e:
        logger.error(f"Failed to create webhook subscription: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.get("/webhooks/subscriptions", tags=["integrations"])
def list_webhook_subscriptions(
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """List all webhook subscriptions."""
    logger.info("Listing webhook subscriptions")

    try:
        subscriptions = WebhookManager.list_subscriptions(active_only=False)
        return {
            "ok": True,
            "count": len(subscriptions),
            "subscriptions": [s.to_dict() for s in subscriptions],
        }
    except Exception as e:
        logger.error(f"Failed to list webhook subscriptions: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.delete("/webhooks/subscriptions/{subscription_id}", tags=["integrations"])
def delete_webhook_subscription(
    subscription_id: str,
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Delete a webhook subscription."""
    logger.info(f"Deleting webhook subscription: {subscription_id}")

    try:
        success = WebhookManager.delete_subscription(subscription_id)
        return {"ok": success, "message": "Subscription deleted" if success else "Not found"}
    except Exception as e:
        logger.error(f"Failed to delete webhook subscription: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/slack/configure", tags=["integrations"])
def configure_slack(
    config: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Configure Slack webhook URL — persisted encrypted, survives restart."""
    logger.info("Configuring Slack")

    try:
        SlackNotifier.set_webhook_url(config.get("webhook_url", ""))
        from revenue_os.services.credentials_vault import save_credentials

        org_id = _integration_org_id()
        save_credentials("slack", "notifications", config, organization_id=org_id)
        return {"ok": True, "message": "Slack configured successfully"}
    except Exception as e:
        logger.error(f"Failed to configure Slack: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/slack/send", tags=["integrations"])
def send_slack_message(
    payload: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Send a message to Slack."""
    logger.info(f"Sending Slack message to {payload.get('channel')}")

    try:
        from revenue_os.integrations.slack import SlackMessage

        message = SlackMessage(
            channel=payload["channel"],
            text=payload["text"],
            blocks=payload.get("blocks"),
            thread_ts=payload.get("thread_ts"),
            icon_emoji=payload.get("icon_emoji"),
            username=payload.get("username", "WorkCrew"),
        )
        success = SlackNotifier.send_message(message)
        return {"ok": success, "message": "Message sent" if success else "Failed to send"}
    except Exception as e:
        logger.error(f"Failed to send Slack message: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/slack/alert", tags=["integrations"])
def send_slack_alert(
    payload: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Send an alert to Slack."""
    logger.info(f"Sending Slack alert to {payload.get('channel')}")

    try:
        success = SlackNotifier.send_alert(
            channel=payload["channel"],
            title=payload["title"],
            message=payload["message"],
            severity=payload.get("severity", "info"),
            fields=payload.get("fields"),
        )
        return {"ok": success, "message": "Alert sent" if success else "Failed to send"}
    except Exception as e:
        logger.error(f"Failed to send Slack alert: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/google-calendar/configure", tags=["integrations"])
def configure_google_calendar(
    config: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Configure Google Calendar — persisted encrypted, survives restart."""
    logger.info("Configuring Google Calendar")

    try:
        credentials = config.get("credentials", {})
        GoogleCalendarClient.configure(credentials)
        from revenue_os.services.credentials_vault import save_credentials

        org_id = _integration_org_id()
        save_credentials("google_calendar", "calendar", credentials, organization_id=org_id)
        return {"ok": True, "message": "Google Calendar configured successfully"}
    except Exception as e:
        logger.error(f"Failed to configure Google Calendar: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/google-calendar/events", tags=["integrations"])
def create_google_calendar_event(
    payload: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Create an event on Google Calendar."""
    logger.info("Creating Google Calendar event")

    try:
        from datetime import datetime as dt

        event = CalendarEvent(
            title=payload["title"],
            description=payload.get("description", ""),
            start_time=dt.fromisoformat(payload["start_time"]),
            end_time=dt.fromisoformat(payload["end_time"]),
            attendees=payload.get("attendees", []),
            location=payload.get("location"),
            calendar_id=payload.get("calendar_id"),
        )
        event_id = GoogleCalendarClient.create_event(event)
        return {
            "ok": event_id is not None,
            "event_id": event_id,
            "message": "Event created" if event_id else "Failed to create event",
        }
    except Exception as e:
        logger.error(f"Failed to create Google Calendar event: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/outlook-calendar/configure", tags=["integrations"])
def configure_outlook_calendar(
    config: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Configure Outlook Calendar — persisted encrypted, survives restart."""
    logger.info("Configuring Outlook Calendar")

    try:
        OutlookCalendarClient.configure(
            tenant_id=config.get("tenant_id", ""),
            access_token=config.get("access_token", ""),
        )
        from revenue_os.services.credentials_vault import save_credentials

        org_id = _integration_org_id()
        save_credentials("outlook_calendar", "calendar", config, organization_id=org_id)
        return {"ok": True, "message": "Outlook Calendar configured successfully"}
    except Exception as e:
        logger.error(f"Failed to configure Outlook Calendar: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.post("/outlook-calendar/events", tags=["integrations"])
def create_outlook_calendar_event(
    payload: dict[str, Any],
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Create an event on Outlook Calendar."""
    logger.info("Creating Outlook Calendar event")

    try:
        from datetime import datetime as dt

        event = CalendarEvent(
            title=payload["title"],
            description=payload.get("description", ""),
            start_time=dt.fromisoformat(payload["start_time"]),
            end_time=dt.fromisoformat(payload["end_time"]),
            attendees=payload.get("attendees", []),
            location=payload.get("location"),
        )
        event_id = OutlookCalendarClient.create_event(event)
        return {
            "ok": event_id is not None,
            "event_id": event_id,
            "message": "Event created" if event_id else "Failed to create event",
        }
    except Exception as e:
        logger.error(f"Failed to create Outlook Calendar event: {str(e)}")
        return {"ok": False, "error": str(e)}


@router.get("/health", tags=["integrations"])
def integrations_health(
    _: str | None = Depends(_verify_api_key),
) -> dict[str, Any]:
    """Get integration system health."""
    logger.info("Checking integrations health")

    subscriptions = len(WebhookManager.list_subscriptions())

    return {
        "ok": True,
        "integrations": {
            "email": "ready",
            "webhooks": "ready",
            "slack": "ready",
            "google_calendar": "ready",
            "outlook_calendar": "ready",
        },
        "webhook_subscriptions": subscriptions,
        "status": "healthy",
    }


_GMAIL_OAUTH_STATE_PURPOSE = "gmail_oauth"
_GMAIL_OAUTH_STATE_TTL = timedelta(minutes=15)


def _sign_gmail_oauth_state(organization_id: str) -> str:
    """Signed OAuth correlation token — not tenant authority by itself."""
    now = datetime.now(timezone.utc)
    payload = {
        "purpose": _GMAIL_OAUTH_STATE_PURPOSE,
        "organization_id": str(organization_id),
        "nonce": str(uuid_lib.uuid4()),
        "iat": now,
        "exp": now + _GMAIL_OAUTH_STATE_TTL,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def _decode_gmail_oauth_state(state: str | None) -> str | None:
    """Return organization_id from a valid signed Gmail OAuth state, else None."""
    if not state or not str(state).strip():
        return None
    try:
        payload = jwt.decode(
            str(state).strip(), settings.SECRET_KEY, algorithms=["HS256"]
        )
    except JWTError:
        return None
    if payload.get("purpose") != _GMAIL_OAUTH_STATE_PURPOSE:
        return None
    raw_org = payload.get("organization_id")
    if not raw_org:
        return None
    try:
        return str(uuid_lib.UUID(str(raw_org)))
    except ValueError:
        return None


def _gmail_oauth_persist_organization_id(*, state: str | None = None) -> str | None:
    """Authenticated session is tenant authority; signed state is CSRF/correlation only.

    State alone never grants persist rights. Missing session fails closed.
    """
    session_org = _integration_org_id()
    if not session_org:
        return None
    persist_org = str(session_org)
    try:
        persist_org = str(uuid_lib.UUID(persist_org))
    except ValueError:
        return None
    state_org = _decode_gmail_oauth_state(state)
    if not state_org:
        return None
    if state_org != persist_org:
        return None
    return persist_org


@router.get("/gmail/authorize", tags=["integrations"])
def gmail_authorize(_: str | None = Depends(_verify_api_key)) -> dict[str, Any]:
    """Build the Google consent URL for the founder to open and approve."""
    from revenue_os.integrations.gmail_sync import build_authorize_url
    from revenue_os.services.credentials_vault import load_credentials

    org_id = _integration_org_id()
    if not org_id:
        raise HTTPException(
            status_code=400,
            detail="Organization context required to authorize Gmail.",
        )
    config = load_credentials(
        "gmail", organization_id=org_id, allow_global_fallback=False
    )
    if not config or not config.get("client_id") or not config.get("redirect_uri"):
        raise HTTPException(
            status_code=400,
            detail="Save the Gmail OAuth client ID, secret, and redirect URI first.",
        )
    signed_state = _sign_gmail_oauth_state(str(org_id))
    url = build_authorize_url(
        config["client_id"], config["redirect_uri"], state=signed_state
    )
    return {"ok": True, "authorize_url": url, "organization_id": str(org_id)}


@router.get("/gmail/callback", tags=["integrations"])
def gmail_callback(
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
) -> Any:
    """OAuth redirect target — exchanges the code for tokens and saves them.

    This is opened directly by the browser (not called via the API client),
    so it returns a small HTML page instead of JSON.

    Tenant authority is the authenticated Founder OS session organization.
    Signed OAuth state is CSRF/correlation only and never grants org rights alone.
    """
    from fastapi.responses import HTMLResponse

    from revenue_os.integrations.gmail_sync import exchange_code_for_tokens
    from revenue_os.services.credentials_vault import load_credentials, save_credentials

    def _page(message: str, ok: bool) -> HTMLResponse:
        color = "#06A77D" if ok else "#D62828"
        return HTMLResponse(
            f"<html><body style='font-family: sans-serif; padding: 3rem; text-align: center;'>"
            f"<h2 style='color: {color}'>{message}</h2>"
            f"<p>You can close this tab and return to the Integrations page.</p>"
            f"</body></html>"
        )

    if error:
        return _page(f"Gmail connection failed: {error}", ok=False)
    if not code:
        return _page("Missing authorization code.", ok=False)

    org_id = _gmail_oauth_persist_organization_id(state=state)
    if not org_id:
        return _page(
            "Organization context required — Gmail was not connected. "
            "Re-open authorize from a signed-in organization session.",
            ok=False,
        )

    config = load_credentials(
        "gmail", organization_id=org_id, allow_global_fallback=False
    )
    if not config:
        return _page("Gmail OAuth client is not configured for this organization.", ok=False)

    tokens = exchange_code_for_tokens(
        code, config["client_id"], config["client_secret"], config["redirect_uri"]
    )
    if not tokens.get("ok"):
        return _page(f"Gmail connection failed: {tokens.get('error')}", ok=False)

    save_credentials(
        "gmail",
        "email",
        {**config, "refresh_token": tokens.get("refresh_token", config.get("refresh_token"))},
        organization_id=org_id,
    )
    return _page("Gmail connected successfully.", ok=True)


@router.post("/gmail/sync", tags=["integrations"])
def gmail_sync_now(_: str | None = Depends(_verify_api_key)) -> dict[str, Any]:
    """Manually trigger Gmail inbox sync for the authenticated organization only."""
    from revenue_os.integrations.gmail_sync import sync_inbox

    org_id = _integration_org_id()
    if not org_id:
        raise HTTPException(
            status_code=400,
            detail="Organization context required for Gmail sync.",
        )
    return sync_inbox(organization_id=str(org_id))
