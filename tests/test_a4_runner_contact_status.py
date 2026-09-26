"""SALES A4 — human-gated runner contact status + score-without-mutation tests."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import runner_api_routers.crm as crm
from revenue_os.models.contact import ContactSource, ContactStatus
from revenue_os.services.lead_scoring_service import (
    LeadScorer,
    apply_contact_status_update,
    score_contact,
)
from runner_api import app

_CONTACT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class _FakeContact:
    def __init__(self, status: ContactStatus = ContactStatus.LEAD, lead_score: int = 0) -> None:
        self.id = UUID(_CONTACT_ID)
        self.first_name = "Test"
        self.last_name = "Lead"
        self.email = "test@example.com"
        self.phone = None
        self.designation = "Founder"
        self.linkedin_url = None
        self.source = ContactSource.WEB_FORM
        self.status = status
        self.lead_score = lead_score
        self.created_at = datetime.now(timezone.utc)
        self.last_contacted_at = None
        self.company = None


class _FakeDB:
    def __init__(self, contact: _FakeContact | None) -> None:
        self._contact = contact
        self.committed = False

    def get(self, _model, _id):  # noqa: ANN001
        return self._contact

    def close(self) -> None:
        return None

    def add(self, _obj) -> None:  # noqa: ANN001
        return None

    def commit(self) -> None:
        self.committed = True

    def refresh(self, _obj) -> None:  # noqa: ANN001
        return None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_lead_scorer_suggest_status_without_mutation() -> None:
    contact = _FakeContact(status=ContactStatus.LEAD, lead_score=10)
    score = LeadScorer.calculate_score(contact)
    suggested = LeadScorer.suggest_status_from_score(score)
    assert contact.status == ContactStatus.LEAD
    assert suggested in (ContactStatus.LEAD, ContactStatus.PROSPECT, ContactStatus.QUALIFIED)


def test_score_contact_does_not_mutate_status() -> None:
    contact = _FakeContact(status=ContactStatus.LEAD, lead_score=0)
    db = _FakeDB(contact)
    payload = score_contact(db, contact)  # type: ignore[arg-type]
    assert payload["status_changed"] is False
    assert contact.status == ContactStatus.LEAD
    assert payload["score"] >= 0
    assert payload["suggested_status"] in {
        ContactStatus.LEAD.value,
        ContactStatus.PROSPECT.value,
        ContactStatus.QUALIFIED.value,
    }


def test_apply_contact_status_update_changes_status() -> None:
    contact = _FakeContact(status=ContactStatus.LEAD)
    db = _FakeDB(contact)
    result = apply_contact_status_update(
        db, contact, ContactStatus.QUALIFIED, requested_by="Krishna Sales"
    )  # type: ignore[arg-type]
    assert result["changed"] is True
    assert result["new_status"] == "qualified"
    assert contact.status == ContactStatus.QUALIFIED
    assert db.committed is True


def test_apply_contact_status_update_same_status_noop() -> None:
    contact = _FakeContact(status=ContactStatus.PROSPECT)
    result = apply_contact_status_update(
        _FakeDB(contact), contact, ContactStatus.PROSPECT, requested_by="Krishna Sales"
    )  # type: ignore[arg-type]
    assert result["changed"] is False
    assert result["new_status"] == "prospect"


def test_runner_score_without_mutation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = _FakeContact(status=ContactStatus.LEAD, lead_score=0)
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(contact))
    events: list = []
    monkeypatch.setattr(crm.EventBus, "publish", lambda e: events.append(e))

    r = client.post(f"/api/v1/crm/contacts/{_CONTACT_ID}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status_changed"] is False
    assert contact.status == ContactStatus.LEAD
    assert body["suggested_status"] in {"lead", "prospect", "qualified"}
    assert events and events[0].data["status_changed"] is False


def test_runner_status_valid_human_change(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = _FakeContact(status=ContactStatus.LEAD)
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(contact))
    events: list = []
    monkeypatch.setattr(crm.EventBus, "publish", lambda e: events.append(e))

    r = client.patch(
        f"/api/v1/crm/contacts/{_CONTACT_ID}/status",
        json={"status": "qualified", "requested_by": "Krishna Founder"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["changed"] is True
    assert body["new_status"] == "qualified"
    assert body["requested_by"] == "Krishna Founder"
    assert contact.status == ContactStatus.QUALIFIED
    assert events and events[0].data["requested_by"] == "Krishna Founder"


def test_runner_status_rejects_agent_requester(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = _FakeContact()
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(contact))
    r = client.patch(
        f"/api/v1/crm/contacts/{_CONTACT_ID}/status",
        json={"status": "qualified", "requested_by": "agent"},
    )
    assert r.status_code == 403
    assert contact.status == ContactStatus.LEAD


def test_runner_status_rejects_ai_prefix(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(_FakeContact()))
    r = client.patch(
        f"/api/v1/crm/contacts/{_CONTACT_ID}/status",
        json={"status": "qualified", "requested_by": "ai:copilot"},
    )
    assert r.status_code == 403


def test_runner_status_rejects_automation_requester(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(_FakeContact()))
    r = client.patch(
        f"/api/v1/crm/contacts/{_CONTACT_ID}/status",
        json={"status": "qualified", "requested_by": "bot"},
    )
    assert r.status_code == 403


@pytest.mark.real_api_auth
def test_runner_status_unauthenticated_when_key_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "secret-a4-key")
    r = client.patch(
        f"/api/v1/crm/contacts/{_CONTACT_ID}/status",
        json={"status": "qualified", "requested_by": "Krishna"},
    )
    assert r.status_code == 401


def test_runner_status_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(None))
    r = client.patch(
        f"/api/v1/crm/contacts/{_CONTACT_ID}/status",
        json={"status": "qualified", "requested_by": "Krishna"},
    )
    assert r.status_code == 404


def test_runner_status_malformed_contact_id(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/crm/contacts/not-a-uuid/status",
        json={"status": "qualified", "requested_by": "Krishna"},
    )
    assert r.status_code == 422


def test_runner_status_invalid_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(_FakeContact()))
    r = client.patch(
        f"/api/v1/crm/contacts/{_CONTACT_ID}/status",
        json={"status": "not_a_real_status", "requested_by": "Krishna"},
    )
    assert r.status_code == 422


def test_runner_status_same_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = _FakeContact(status=ContactStatus.QUALIFIED)
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(contact))
    r = client.patch(
        f"/api/v1/crm/contacts/{_CONTACT_ID}/status",
        json={"status": "qualified", "requested_by": "Krishna"},
    )
    assert r.status_code == 200
    assert r.json()["changed"] is False


def test_high_score_does_not_auto_qualify_via_score_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = _FakeContact(status=ContactStatus.LEAD, lead_score=0)
    contact.email = "founder@company.com"
    contact.linkedin_url = "https://linkedin.com/in/test"
    contact.source = ContactSource.REFERRAL
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(contact))
    r = client.post(f"/api/v1/crm/contacts/{_CONTACT_ID}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["status_changed"] is False
    assert contact.status == ContactStatus.LEAD
    if body["score"] >= 70:
        assert body["suggested_status"] == "qualified"
