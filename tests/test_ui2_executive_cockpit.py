"""UI2 — Executive Cockpit v1 focused tests."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401 — register tables
import revenue_os.services.tenant_resolution as tenant_resolution_mod
import runner_api_routers.identity as identity_mod
from revenue_os.auth import hash_password
from revenue_os.models.base import Base
from revenue_os.models.contact import ContactSource, ContactStatus
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.cockpit_read_model import build_cockpit_snapshot
from runner_api import app

_OPERATOR_EMAIL = "ui2-operator@example.com"
_OPERATOR_PASSWORD = "correct-horse-battery"

_DEMAND_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CONTACT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_OPERATOR = "Krishna Founder"


class _FakeContact:
    def __init__(self) -> None:
        self.id = UUID(_CONTACT_ID)
        self.first_name = "Cockpit"
        self.last_name = "Lead"
        self.email = "cockpit@example.com"
        self.status = ContactStatus.LEAD
        self.lead_score = 75
        self.phone = None
        self.designation = None
        self.linkedin_url = None
        self.source = ContactSource.WEB_FORM
        self.created_at = datetime.now(timezone.utc)
        self.company = None
        self.organization_id = None


class _FakeAgentLog:
    def __init__(
        self,
        action_type: str,
        target_id: str,
        detail: dict | None = None,
        organization_id: str | None = None,
    ) -> None:
        self.action_type = action_type
        self.target_id = target_id
        self.detail = detail or {}
        self.organization_id = organization_id
        self.created_at = datetime.now(timezone.utc)


class _FakeDB:
    def __init__(self) -> None:
        self.contacts = [_FakeContact()]
        self.logs: list = []
        self.deals: list = []

    def query(self, model):  # noqa: ANN001
        return _FakeQuery(self, model)

    def get(self, _model, _id):  # noqa: ANN001
        if str(_id) == _CONTACT_ID:
            return self.contacts[0]
        return None

    def add(self, _obj) -> None:  # noqa: ANN001
        return None

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeQuery:
    def __init__(self, db: _FakeDB, model) -> None:  # noqa: ANN001
        self._db = db
        self._model = model
        self._filters: list = []

    def filter(self, *args) -> _FakeQuery:  # noqa: ANN001
        self._filters.extend(args)
        return self

    def order_by(self, *_a) -> _FakeQuery:  # noqa: ANN001
        return self

    def all(self):  # noqa: ANN001
        name = getattr(self._model, "__name__", str(self._model))
        if name == "Contact":
            return self._db.contacts
        if name == "Deal":
            return self._db.deals
        if name == "AgentActionLog":
            action_type = None
            for f in self._filters:
                left = getattr(f, "left", None)
                right = getattr(f, "right", None)
                if left is not None and hasattr(left, "key") and left.key == "action_type":
                    action_type = getattr(right, "value", right)
            return [r for r in self._db.logs if not action_type or r.action_type == action_type]
        return []

    def first(self):  # noqa: ANN001
        rows = self.all()
        return rows[0] if rows else None


@pytest.fixture
def client(cms_client: TestClient) -> TestClient:
    return cms_client


@pytest.fixture
def human_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> str:
    """SaaS S2+: mutations resolve tenant from a real logged-in session with an
    active OrganizationMembership — FOUNDER_OS_OPERATOR_NAME alone is legacy
    fallback only and no longer satisfies require_tenant_mutation()."""
    engine = create_engine(f"sqlite:///{tmp_path / 'ui2_identity.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(identity_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(tenant_resolution_mod, "SessionLocal", session_factory)

    db = session_factory()
    try:
        user = User(
            email=_OPERATOR_EMAIL,
            hashed_password=hash_password(_OPERATOR_PASSWORD),
            full_name=_OPERATOR,
            role="owner",
            is_active=1,
        )
        db.add(user)
        db.flush()
        org = Organization(name="UI2 Org", slug="ui2-org", status=OrganizationStatus.ACTIVE)
        db.add(org)
        db.flush()
        db.add(
            OrganizationMembership(
                user_id=user.id,
                organization_id=org.id,
                role="owner",
                status=MembershipStatus.ACTIVE,
            )
        )
        db.commit()
        org_id = str(org.id)
    finally:
        db.close()

    r = client.post(
        "/login",
        data={"email": _OPERATOR_EMAIL, "password": _OPERATOR_PASSWORD, "next": "/cockpit"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return org_id


def test_cockpit_route_loads(client: TestClient) -> None:
    r = client.get("/cockpit")
    assert r.status_code == 200
    assert 'data-testid="panel-attention"' in r.text
    assert "Executive Cockpit" in r.text


def test_shell_navigation_links_cockpit(client: TestClient) -> None:
    shell = client.get("/")
    assert shell.status_code == 200
    assert 'href="/cockpit"' not in shell.text
    assert ">Executive Cockpit<" not in shell.text
    route = client.get("/cockpit")
    assert route.status_code == 200
    assert "Executive Cockpit" in route.text


def test_cockpit_unauthenticated_follows_open_dev_pattern(client: TestClient) -> None:
    """UI routes follow existing open-dev pattern (no separate UI auth layer)."""
    r = client.get("/cockpit")
    assert r.status_code == 200


def test_cockpit_panels_render(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "revenue_os.services.cockpit_read_model.build_cockpit_snapshot",
        lambda *args, **kwargs: {
            "ok": True,
            "generated_at": "2026-08-13T00:00:00Z",
            "panels": {
                "attention": {"state": "ok", "message": "", "data": {"items": [], "count": 0}},
                "sales": {"state": "ok", "message": "", "data": {"contact_total": 2}},
                "marketing_seo": {
                    "state": "ok",
                    "message": "",
                    "data": {
                        "readiness": {"ready_count": 0, "blocked_count": 0, "production_seo_activation": "BLOCKED"},
                        "technical": {"score": 0, "pass_count": 0, "warn_count": 0},
                        "qualified_demand_pending": 0,
                        "social": {"state": "blocked", "message": "test"},
                    },
                },
                "commercial_flow": {"state": "ok", "message": "", "data": {"stages": []}},
                "governance": {
                    "state": "ok",
                    "message": "",
                    "data": {
                        "authority_remediation": "PASS",
                        "ui1_1_bypasses_closed": "3/3",
                        "heartbeat": {"enabled": False, "jobs_registered": 0},
                        "integration_readiness": {"crm_spa": "UNMOUNTED", "production_seo": "BLOCKED"},
                        "frozen_baselines": [],
                    },
                },
            },
            "errors": [],
        },
    )
    r = client.get("/cockpit")
    assert 'data-testid="panel-sales"' in r.text


def test_empty_state_renders_honestly(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "runner_api_routers.ui.build_cockpit_snapshot",
        lambda *args, **kwargs: {
            "ok": True,
            "generated_at": "now",
            "panels": {
                "attention": {
                    "state": "empty",
                    "message": "No items requiring founder attention",
                    "data": {"items": [], "count": 0},
                },
                "sales": {
                    "state": "ok",
                    "message": "",
                    "data": {
                        "contact_total": 0,
                        "deal_total": 0,
                        "contacts_by_status": {},
                        "high_score_review": [],
                    },
                },
                "marketing_seo": {
                    "state": "ok",
                    "message": "",
                    "data": {
                        "readiness": {"ready_count": 0, "blocked_count": 0, "production_seo_activation": "BLOCKED"},
                        "technical": {"score": 0, "pass_count": 0, "warn_count": 0},
                        "qualified_demand_pending": 0,
                        "social": {"state": "blocked", "message": "test"},
                    },
                },
                "commercial_flow": {"state": "ok", "message": "", "data": {"stages": []}},
                "governance": {
                    "state": "ok",
                    "message": "",
                    "data": {
                        "authority_remediation": "PASS",
                        "ui1_1_bypasses_closed": "3/3",
                        "heartbeat": {"enabled": False, "jobs_registered": 0},
                        "integration_readiness": {"crm_spa": "UNMOUNTED", "production_seo": "BLOCKED"},
                        "frozen_baselines": [],
                    },
                },
            },
            "errors": [],
        },
    )
    r = client.get("/cockpit")
    assert 'data-testid="attention-empty"' in r.text


def test_partial_state_db_unavailable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "runner_api_routers.ui.build_cockpit_snapshot",
        lambda *args, **kwargs: {
            "ok": False,
            "generated_at": "now",
            "panels": {
                "attention": {"state": "unavailable", "message": "Sales database unavailable", "data": {}},
                "sales": {"state": "unavailable", "message": "Sales database unavailable", "data": {}},
                "marketing_seo": {
                    "state": "ok",
                    "message": "",
                    "data": {
                        "readiness": {"ready_count": 0, "blocked_count": 0, "production_seo_activation": "BLOCKED"},
                        "technical": {"score": 0, "pass_count": 0, "warn_count": 0},
                        "qualified_demand_pending": 0,
                        "social": {"state": "blocked", "message": "test"},
                    },
                },
                "commercial_flow": {"state": "ok", "message": "", "data": {"stages": []}},
                "governance": {
                    "state": "ok",
                    "message": "",
                    "data": {
                        "authority_remediation": "PASS",
                        "ui1_1_bypasses_closed": "3/3",
                        "heartbeat": {"enabled": False, "jobs_registered": 0},
                        "integration_readiness": {"crm_spa": "UNMOUNTED", "production_seo": "BLOCKED"},
                        "frozen_baselines": [],
                    },
                },
            },
            "errors": ["sales_db"],
        },
    )
    r = client.get("/cockpit")
    assert 'data-testid="sales-unavailable"' in r.text


def test_api_failure_not_fake_zero(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "runner_api_routers.ui.build_cockpit_snapshot",
        lambda *args, **kwargs: {
            "ok": False,
            "generated_at": "now",
            "panels": {
                "attention": {"state": "unavailable", "message": "DB down", "data": {}},
                "sales": {"state": "unavailable", "message": "Sales database unavailable — counts not shown", "data": {}},
                "marketing_seo": {"state": "error", "message": "SEO failed", "data": {}},
                "commercial_flow": {"state": "ok", "message": "", "data": {"stages": []}},
                "governance": {
                    "state": "ok",
                    "message": "",
                    "data": {
                        "authority_remediation": "PASS",
                        "ui1_1_bypasses_closed": "3/3",
                        "heartbeat": {"enabled": False, "jobs_registered": 0},
                        "integration_readiness": {"crm_spa": "UNMOUNTED", "production_seo": "BLOCKED"},
                        "frozen_baselines": [],
                    },
                },
            },
            "errors": ["seo"],
        },
    )
    r = client.get("/cockpit")
    assert 'data-testid="seo-error"' in r.text
    assert "Sales database unavailable" in r.text


def test_qualified_demand_accept_valid_human(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, human_session: str
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    monkeypatch.setattr(
        "runner_api_routers.cockpit.accept_qualified_demand",
        lambda db, did, rb, notes="", organization_id=None: {
            "ok": True,
            "idempotent": False,
            "demand_id": did,
            "contact_id": _CONTACT_ID,
            "created": True,
            "deal_created": False,
        },
    )
    db = _FakeDB()
    db.logs.append(_FakeAgentLog("qualified_demand_handoff", _DEMAND_ID, organization_id=human_session))
    monkeypatch.setattr("runner_api_routers.cockpit.SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/cockpit/actions/qualified-demand/accept",
        json={"demand_id": _DEMAND_ID},
    )
    assert r.status_code == 200
    assert r.json()["operator"] == _OPERATOR


def test_qualified_demand_accept_unauthorized_no_operator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FOUNDER_OS_OPERATOR_NAME", raising=False)
    r = client.post(
        "/api/v1/cockpit/actions/qualified-demand/accept",
        json={"demand_id": _DEMAND_ID},
    )
    assert r.status_code == 503


def test_qualified_demand_accept_agent_operator_blocked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", "agent:hermes")
    r = client.post(
        "/api/v1/cockpit/actions/qualified-demand/accept",
        json={"demand_id": _DEMAND_ID},
    )
    assert r.status_code == 503


def test_qualified_demand_accept_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, human_session: str
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    monkeypatch.setattr(
        "runner_api_routers.cockpit.accept_qualified_demand",
        lambda db, did, rb, notes="", organization_id=None: {
            "ok": True,
            "idempotent": True,
            "demand_id": did,
            "contact_id": _CONTACT_ID,
        },
    )
    db = _FakeDB()
    db.logs.append(_FakeAgentLog("qualified_demand_handoff", _DEMAND_ID, organization_id=human_session))
    monkeypatch.setattr("runner_api_routers.cockpit.SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/cockpit/actions/qualified-demand/accept",
        json={"demand_id": _DEMAND_ID},
    )
    assert r.status_code == 200
    assert r.json()["idempotent"] is True


def test_contact_status_valid_human(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    contact = _FakeContact()
    db = _FakeDB()
    db.contacts = [contact]

    def _apply(db_arg, c, status, *, requested_by):  # noqa: ANN001
        c.status = status
        return {"changed": True, "old_status": "lead", "new_status": status.value}

    monkeypatch.setattr("runner_api_routers.cockpit.apply_contact_status_update", _apply)
    monkeypatch.setattr("runner_api_routers.cockpit.SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/cockpit/actions/contact-status",
        json={"contact_id": _CONTACT_ID, "status": "qualified"},
    )
    assert r.status_code == 200
    assert r.json()["operator"] == _OPERATOR


def test_contact_status_unauthorized_no_operator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FOUNDER_OS_OPERATOR_NAME", raising=False)
    r = client.post(
        "/api/v1/cockpit/actions/contact-status",
        json={"contact_id": _CONTACT_ID, "status": "qualified"},
    )
    assert r.status_code == 503


def test_contact_status_agent_operator_blocked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", "AI")
    r = client.post(
        "/api/v1/cockpit/actions/contact-status",
        json={"contact_id": _CONTACT_ID, "status": "qualified"},
    )
    assert r.status_code == 503


def test_contact_status_invalid_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    monkeypatch.setattr("runner_api_routers.cockpit.SessionLocal", lambda: _FakeDB())
    r = client.post(
        "/api/v1/cockpit/actions/contact-status",
        json={"contact_id": _CONTACT_ID, "status": "not_a_status"},
    )
    assert r.status_code == 422


def test_spoofed_requested_by_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, human_session: str
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    captured: list[str] = []

    def _accept(db, did, rb, notes="", organization_id=None):  # noqa: ANN001
        captured.append(rb)
        return {"ok": True, "idempotent": True, "demand_id": did}

    monkeypatch.setattr("runner_api_routers.cockpit.accept_qualified_demand", _accept)
    db = _FakeDB()
    db.logs.append(_FakeAgentLog("qualified_demand_handoff", _DEMAND_ID, organization_id=human_session))
    monkeypatch.setattr("runner_api_routers.cockpit.SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/cockpit/actions/qualified-demand/accept",
        json={"demand_id": _DEMAND_ID, "requested_by": "agent:spoof"},
    )
    assert r.status_code == 200
    assert captured == [_OPERATOR]


def test_deferred_actions_not_exposed(client: TestClient) -> None:
    r = client.get("/cockpit")
    text = r.text.lower()
    assert "reject intake" not in text
    assert "data-testid=\"deal-stage" not in text
    assert "editorial approve" not in text
    assert "publishing promote" not in text
    assert "marketing handoff" not in text


def test_no_crm_spa_dependency(client: TestClient) -> None:
    r = client.get("/cockpit")
    assert "/app" not in r.text or "CRM SPA" not in r.text
    assert "vite" not in r.text.lower()


def test_commercial_flow_revenue_honest(client: TestClient) -> None:
    snap = build_cockpit_snapshot()
    stages = snap["panels"]["commercial_flow"]["data"]["stages"]
    revenue = next(s for s in stages if "Revenue" in s["label"])
    assert revenue["state"] == "emerging"
    assert "NOT YET ACTIVE" in revenue["detail"]


def test_snapshot_endpoint(client: TestClient) -> None:
    r = client.get("/api/v1/cockpit/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert "panels" in body
    assert "attention" in body["panels"]


def test_founder_os_shell_branding(client: TestClient) -> None:
    r = client.get("/cockpit")
    assert "Founder OS" in r.text


def test_high_score_display_does_not_mutate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = _FakeContact()
    db = _FakeDB()
    db.contacts = [contact]
    monkeypatch.setattr("revenue_os.services.cockpit_read_model.SessionLocal", lambda: db)
    snap = build_cockpit_snapshot()
    assert contact.status == ContactStatus.LEAD


def test_accept_no_deal_create(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, human_session: str
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    monkeypatch.setattr(
        "runner_api_routers.cockpit.accept_qualified_demand",
        lambda db, did, rb, notes="", organization_id=None: {
            "ok": True,
            "deal_created": False,
            "commercial_outcome_emitted": False,
        },
    )
    db = _FakeDB()
    db.logs.append(_FakeAgentLog("qualified_demand_handoff", _DEMAND_ID, organization_id=human_session))
    monkeypatch.setattr("runner_api_routers.cockpit.SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/cockpit/actions/qualified-demand/accept",
        json={"demand_id": _DEMAND_ID},
    )
    assert r.json()["deal_created"] is False
