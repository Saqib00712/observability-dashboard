"""Phase 4: Prometheus metrics -- exposition, labels, cardinality, errors."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client.parser import text_string_to_metric_families

from obsdash.main import create_app
from obsdash.metrics import Metrics, status_class
from obsdash.middleware import RequestContextMiddleware


def _samples(client):
    text = client.get("/metrics").text
    return [s for fam in text_string_to_metric_families(text) for s in fam.samples]


def _value(samples, name, **labels):
    for s in samples:
        if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
            return s.value
    return None


def _client():
    return TestClient(create_app(TracerProvider()), raise_server_exceptions=False)


def test_status_class():
    assert status_class(200) == "2xx" and status_class(404) == "4xx" and status_class(503) == "5xx"


def test_metrics_endpoint_serves_prometheus_text():
    resp = _client().get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "obsdash_http_requests_total" in resp.text or "# HELP" in resp.text


def test_request_increments_counter_and_histogram():
    c = _client()
    c.get("/health")
    c.get("/health")
    s = _samples(c)
    assert _value(s, "obsdash_http_requests_total", method="GET", route="/health", status_class="2xx") == 2
    assert _value(s, "obsdash_http_request_duration_seconds_count", method="GET", route="/health") == 2


def test_unknown_paths_collapse_into_one_unmatched_label():
    c = _client()
    for i in range(5):
        assert c.get(f"/random-scanner-path-{i}").status_code == 404
    s = _samples(c)
    assert _value(s, "obsdash_http_requests_total", route="unmatched", status_class="4xx") == 5
    assert not any("random-scanner" in str(x.labels) for x in s)


def test_route_template_not_raw_path_is_the_label():
    app = FastAPI()
    app.state.tracer_provider = TracerProvider()
    app.state.metrics = Metrics()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    c = TestClient(app)
    c.get("/items/1")
    c.get("/items/2")
    samples = [s for f in text_string_to_metric_families(app.state.metrics.render()[0].decode()) for s in f.samples]
    assert _value(samples, "obsdash_http_requests_total", route="/items/{item_id}") == 2
    assert not any(s.labels.get("route") in ("/items/1", "/items/2") for s in samples)


def test_exception_counts_as_5xx_and_in_flight_returns_to_zero():
    app = FastAPI()
    app.state.tracer_provider = TracerProvider()
    app.state.metrics = Metrics()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/boom")
    def boom():
        raise ValueError("x")

    TestClient(app, raise_server_exceptions=False).get("/boom")
    samples = [s for f in text_string_to_metric_families(app.state.metrics.render()[0].decode()) for s in f.samples]
    assert _value(samples, "obsdash_http_requests_total", route="/boom", status_class="5xx") == 1
    assert _value(samples, "obsdash_http_requests_in_flight") == 0


def test_two_apps_have_independent_registries():
    a, b = _client(), _client()
    a.get("/health")
    assert _value(_samples(b), "obsdash_http_requests_total", route="/health") is None
