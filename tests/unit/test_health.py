"""
Phase 1: the app starts and /health responds correctly. This is the
smallest possible test that still proves something real -- not just
"the module imports" but "a request actually goes through FastAPI's
routing and comes back with the right shape."
"""

from fastapi.testclient import TestClient

from obsdash.main import create_app


def test_health_returns_ok_status():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_response_shape():
    client = TestClient(create_app())
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "obsdash"
    assert "environment" in body
    assert "version" in body


def test_unknown_route_returns_404():
    client = TestClient(create_app())
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
