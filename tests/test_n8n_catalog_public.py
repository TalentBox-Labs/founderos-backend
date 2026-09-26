"""Bounded classification: GET /webhooks/n8n/catalog is a static name list."""

from __future__ import annotations

from fastapi.testclient import TestClient

from runner_api import app
from runner_api_routers.n8n_webhooks import INBOUND_CATALOG, OUTBOUND_CATALOG

_FORBIDDEN_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "database_url",
    "organization_id",
    "tenant_id",
    "email",
}


def test_n8n_catalog_is_static_and_unauthenticated() -> None:
    client = TestClient(app)
    response = client.get("/webhooks/n8n/catalog")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["inbound"]["events"] == INBOUND_CATALOG
    assert body["outbound"]["webhooks"] == OUTBOUND_CATALOG
    assert set(body) == {"ok", "inbound", "outbound"}

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert key.lower() not in _FORBIDDEN_KEYS
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(body)
