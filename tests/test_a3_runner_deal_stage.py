"""SALES A3 — human-gated runner deal stage update tests."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import runner_api_routers.crm as crm
from revenue_os.models.deal import DealStage
from revenue_os.services.deal_automation_service import apply_deal_stage_update
from runner_api import app

_DEAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _FakeDeal:
    def __init__(self, stage: DealStage = DealStage.DISCOVERY) -> None:
        self.id = UUID(_DEAL_ID)
        self.name = "Test Deal"
        self.stage = stage
        self.probability = 20
        self.value = 1000.0
        self.currency = "USD"
        self.contact_id = None
        self.expected_close_date = None
        self.closed_at = None
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)


class _FakeDB:
    def __init__(self, deal: _FakeDeal | None) -> None:
        self._deal = deal

    def get(self, _model, _id):  # noqa: ANN001
        return self._deal

    def close(self) -> None:
        return None

    def add(self, _obj) -> None:  # noqa: ANN001
        return None

    def commit(self) -> None:
        return None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_apply_deal_stage_update_reuses_advance_and_sets_closed_at() -> None:
    deal = _FakeDeal(DealStage.NEGOTIATION)
    db = _FakeDB(deal)
    result = apply_deal_stage_update(
        db, deal, DealStage.CLOSED_WON, requested_by="Krishna Sales"
    )  # type: ignore[arg-type]
    assert result["changed"] is True
    assert result["new_stage"] == "closed_won"
    assert result["commercial_outcome_emitted"] is False
    assert deal.stage == DealStage.CLOSED_WON
    assert deal.probability == 100
    assert deal.closed_at is not None


def test_apply_deal_stage_update_same_stage_noop() -> None:
    deal = _FakeDeal(DealStage.PROPOSAL)
    deal.probability = 60
    result = apply_deal_stage_update(
        _FakeDB(deal), deal, DealStage.PROPOSAL, requested_by="Krishna Sales"
    )  # type: ignore[arg-type]
    assert result["changed"] is False
    assert result["new_stage"] == "proposal"


def test_apply_deal_stage_update_rejects_reopen() -> None:
    deal = _FakeDeal(DealStage.CLOSED_WON)
    with pytest.raises(ValueError, match="terminal"):
        apply_deal_stage_update(
            _FakeDB(deal), deal, DealStage.DISCOVERY, requested_by="Krishna Sales"
        )  # type: ignore[arg-type]


def test_apply_deal_stage_update_rejects_recruitment_stage() -> None:
    deal = _FakeDeal(DealStage.DISCOVERY)
    with pytest.raises(ValueError, match="Invalid sales stage"):
        apply_deal_stage_update(
            _FakeDB(deal), deal, DealStage.INTERVIEW, requested_by="Krishna Sales"
        )  # type: ignore[arg-type]


def test_runner_stage_valid_human_transition(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal = _FakeDeal(DealStage.DISCOVERY)
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(deal))
    events: list = []
    monkeypatch.setattr(crm.EventBus, "publish", lambda e: events.append(e))

    r = client.patch(
        f"/api/v1/crm/deals/{_DEAL_ID}/stage",
        json={"stage": "qualified", "requested_by": "Krishna Founder"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["changed"] is True
    assert body["new_stage"] == "qualified"
    assert body["requested_by"] == "Krishna Founder"
    assert body["commercial_outcome_emitted"] is False
    assert body["deal"]["probability"] == 40
    assert events and events[0].data["requested_by"] == "Krishna Founder"


def test_runner_stage_rejects_agent_requester(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal = _FakeDeal()
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(deal))
    r = client.patch(
        f"/api/v1/crm/deals/{_DEAL_ID}/stage",
        json={"stage": "proposal", "requested_by": "agent"},
    )
    assert r.status_code == 403
    assert deal.stage == DealStage.DISCOVERY


def test_runner_stage_rejects_ai_prefix(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(_FakeDeal()))
    r = client.patch(
        f"/api/v1/crm/deals/{_DEAL_ID}/stage",
        json={"stage": "proposal", "requested_by": "ai:copilot"},
    )
    assert r.status_code == 403


@pytest.mark.real_api_auth
def test_runner_stage_unauthenticated_when_key_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "secret-a3-key")
    r = client.patch(
        f"/api/v1/crm/deals/{_DEAL_ID}/stage",
        json={"stage": "proposal", "requested_by": "Krishna"},
    )
    assert r.status_code == 401


@pytest.mark.real_api_auth
def test_runner_stage_wrong_api_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "secret-a3-key")
    r = client.patch(
        f"/api/v1/crm/deals/{_DEAL_ID}/stage",
        headers={"Authorization": "Bearer wrong"},
        json={"stage": "proposal", "requested_by": "Krishna"},
    )
    assert r.status_code == 401


def test_runner_stage_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(None))
    r = client.patch(
        f"/api/v1/crm/deals/{_DEAL_ID}/stage",
        json={"stage": "proposal", "requested_by": "Krishna"},
    )
    assert r.status_code == 404


def test_runner_stage_malformed_deal_id(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/crm/deals/not-a-uuid/stage",
        json={"stage": "proposal", "requested_by": "Krishna"},
    )
    assert r.status_code == 422


def test_runner_stage_malformed_stage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(_FakeDeal()))
    r = client.patch(
        f"/api/v1/crm/deals/{_DEAL_ID}/stage",
        json={"stage": "not_a_real_stage", "requested_by": "Krishna"},
    )
    assert r.status_code == 422


def test_runner_stage_same_stage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal = _FakeDeal(DealStage.QUALIFIED)
    deal.probability = 40
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(deal))
    r = client.patch(
        f"/api/v1/crm/deals/{_DEAL_ID}/stage",
        json={"stage": "qualified", "requested_by": "Krishna"},
    )
    assert r.status_code == 200
    assert r.json()["changed"] is False


def test_runner_stage_closed_won_no_commercial_outcome(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal = _FakeDeal(DealStage.NEGOTIATION)
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(deal))
    r = client.patch(
        f"/api/v1/crm/deals/{_DEAL_ID}/stage",
        json={"stage": "closed_won", "requested_by": "Krishna Founder"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["new_stage"] == "closed_won"
    assert body["commercial_outcome_emitted"] is False
    assert body["deal"]["closed_at"] is not None


def test_runner_stage_rejects_reopen_via_api(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal = _FakeDeal(DealStage.CLOSED_LOST)
    monkeypatch.setattr(crm, "SessionLocal", lambda: _FakeDB(deal))
    r = client.patch(
        f"/api/v1/crm/deals/{_DEAL_ID}/stage",
        json={"stage": "discovery", "requested_by": "Krishna"},
    )
    assert r.status_code == 422
