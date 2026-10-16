"""
Phase 2: RequestContextMiddleware over real HTTP requests (via FastAPI's
TestClient) -- request ID propagation/generation, and safe handling of
an unhandled exception in a route.

A route that deliberately raises is built in THIS test file (not added
to the real app in main.py) -- obsdash's actual routes shouldn't carry
a permanent "crash on purpose" endpoint just so a test can exercise the
error path.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider

from obsdash.main import create_app
from obsdash.metrics import Metrics
from obsdash.middleware import RequestContextMiddleware


def _app_with_boom_route() -> FastAPI:
    app = FastAPI()
    app.state.tracer_provider = TracerProvider()
    app.state.metrics = Metrics()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/boom")
    def boom():
        raise ValueError("a specific internal detail that must not reach the client")

    return app


def test_response_carries_an_x_request_id_header():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert "X-Request-ID" in resp.headers
    assert len(resp.headers["X-Request-ID"]) > 0


def test_incoming_request_id_is_honored_not_overwritten():
    client = TestClient(create_app())
    resp = client.get("/health", headers={"X-Request-ID": "caller-supplied-id-123"})
    assert resp.headers["X-Request-ID"] == "caller-supplied-id-123"


def test_two_requests_without_an_incoming_id_get_different_ids():
    client = TestClient(create_app())
    first = client.get("/health").headers["X-Request-ID"]
    second = client.get("/health").headers["X-Request-ID"]
    assert first != second


def test_unhandled_exception_returns_500_with_request_id_not_a_stack_trace():
    client = TestClient(_app_with_boom_route(), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "internal_server_error"
    assert "request_id" in body
    # The real exception message must never reach the client.
    assert "a specific internal detail" not in resp.text
    assert "Traceback" not in resp.text


def test_unhandled_exception_response_still_carries_the_request_id_header():
    client = TestClient(_app_with_boom_route(), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.headers["X-Request-ID"] == resp.json()["request_id"]
