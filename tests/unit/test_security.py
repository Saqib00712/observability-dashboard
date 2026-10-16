"""Phase 10: redaction, access control, leakage canaries, cardinality, retention,
timeouts, exporter-failure handling, non-blocking export."""

import base64
import hashlib
import json
import logging
import re
import time

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client.parser import text_string_to_metric_families

from obsdash.ai_telemetry import MAX_DISTINCT_TARGETS, AITelemetry, OperationKind
from obsdash.alert_engine import AlertStateStore
from obsdash.config import Settings, get_settings
from obsdash.main import create_app, dashboard_csp
from obsdash.otel_setup import build_tracer_provider
from obsdash.span_redaction import RedactingSpanExporter
from obsdash.redaction import OTHER, luhn_valid, redact_text, sanitize_label
from obsdash.trace_store import QueryTimeout, TraceStore, TraceStoreExporter
from obsdash.tracing import get_tracer

TOKEN = "correct-horse-battery-staple-123"


# ---------------- redaction ----------------
@pytest.mark.parametrize("text,gone", [
    ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
    ("key is sk-ABCDEFGHIJKLMNOPQRSTUVWX", "sk-ABCDEF"),
    ("password=hunter2 next", "hunter2"),
    ('{"api_key": "topsecretvalue"}', "topsecretvalue"),
    ("mail a.b@example.com now", "a.b@example.com"),
    ("card 4111 1111 1111 1111 end", "4111"),
    ("card 4111-1111-1111-1111", "4111"),
    ("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "eyJhbGci"),
    ("AKIAABCDEFGHIJKLMNOP", "AKIAABCD"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdef"),
])
def test_redacts_recognisable_secrets(text, gone):
    assert gone not in redact_text(text)


@pytest.mark.parametrize("text", [
    "input_tokens=10 max_tokens: 5", "order 1234567890123456 shipped",  # fails Luhn -> not a card
    "GET /items/42 took 12 ms", "model_call fake-llm",
])
def test_leaves_ordinary_text_alone(text):
    assert redact_text(text) == text


def test_luhn():
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112")


def test_redact_keeps_surrounding_text_and_truncates():
    assert redact_text("card 4111 1111 1111 1111 end") == "card [redacted-card] end"
    assert len(redact_text("a" * 10_000, max_len=50)) == 50
    assert redact_text(123) == 123  # non-strings pass through untouched


def test_redaction_is_fast_on_hostile_input():
    t = time.perf_counter()
    redact_text("1 " * 50_000 + "a" * 50_000)
    redact_text("password=" + "x" * 100_000)
    assert time.perf_counter() - t < 1.0  # bounded work (input is capped before regex)


def test_sanitize_label():
    assert sanitize_label("gpt\n\x00-4") == "gpt-4"
    assert len(sanitize_label("x" * 500)) == 64
    assert sanitize_label("   ") == "unknown"
    assert "@" not in sanitize_label("model-a.b@example.com")


# ---------------- access control ----------------
def _client(monkeypatch, token=None):
    if token:
        monkeypatch.setenv("API_TOKEN", token)
    get_settings.cache_clear()
    try:
        app = create_app(TracerProvider(), TraceStore(":memory:"), AlertStateStore(":memory:"))
    finally:
        monkeypatch.delenv("API_TOKEN", raising=False)
        get_settings.cache_clear()
    return TestClient(app)


PROTECTED = ["/api/overview", "/api/errors", "/api/traces", "/api/costs/summary", "/api/alerts", "/metrics"]


def test_open_when_no_token_configured(monkeypatch):
    c = _client(monkeypatch)
    assert all(c.get(p).status_code == 200 for p in PROTECTED)


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_routes_require_token(monkeypatch, path):
    c = _client(monkeypatch, TOKEN)
    r = c.get(path)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in r.text
    assert c.get(path, headers={"Authorization": "Bearer wrong-token-wrong-token"}).status_code == 401
    assert c.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert c.get(path, headers={"X-API-Key": TOKEN}).status_code == 200


def test_health_and_dashboard_shell_stay_open(monkeypatch):
    c = _client(monkeypatch, TOKEN)
    assert c.get("/health").status_code == 200 and c.get("/dashboard").status_code == 200


def test_token_in_url_is_not_accepted(monkeypatch):
    c = _client(monkeypatch, TOKEN)
    assert c.get(f"/api/overview?token={TOKEN}").status_code == 401
    assert c.get("/api/overview", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401


def test_short_token_refused_at_startup():
    with pytest.raises(ValueError, match="at least 16"):
        Settings(api_token="short")


def test_secrets_never_appear_in_settings_repr():
    s = Settings(api_token=TOKEN, alert_webhook_url="https://hooks.example.com/SECRETPATH")
    assert TOKEN not in repr(s) and "SECRETPATH" not in repr(s) and TOKEN not in str(s.model_dump())


def test_warns_when_open_on_non_loopback_host(monkeypatch):
    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    log = logging.getLogger("obsdash.app")
    h = H()
    log.addHandler(h)
    monkeypatch.setenv("HOST", "0.0.0.0")
    get_settings.cache_clear()
    try:
        create_app(TracerProvider(), TraceStore(":memory:"), AlertStateStore(":memory:"))
    finally:
        log.removeHandler(h)
        monkeypatch.delenv("HOST")
        get_settings.cache_clear()
    assert "api_unauthenticated_on_non_loopback_host" in seen


def test_default_host_is_loopback():
    assert Settings().host == "127.0.0.1"


# ---------------- headers / CSP ----------------
def test_security_headers_on_api_and_error_responses(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/overview")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"

    app = c.app

    @app.get("/boom")
    def boom():
        raise ValueError("x")

    r500 = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert r500.status_code == 500 and r500.headers["x-content-type-options"] == "nosniff"


def test_dashboard_csp_is_strict_and_hash_matches_the_page(monkeypatch):
    r = _client(monkeypatch).get("/dashboard")
    csp = r.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    script = re.search(r"<script>(.*?)</script>", r.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in csp
    assert csp == dashboard_csp(r.text)
    assert 'style="' not in r.text and "onclick=" not in r.text  # nothing the CSP would block


# ---------------- sensitive data must not leak ----------------
CANARIES = ["hunter2-canary", "CANARYabcdefghijklmnopqrst", "canary.user@example.com", "4111", "CANARYTOKEN"]


def test_no_canary_secret_reaches_logs_spans_store_metrics_or_api(capsys):
    exporter = InMemorySpanExporter()   # stands in for ANY downstream exporter (console / OTLP collector)
    store = TraceStore(":memory:")
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(exporter)))
    provider.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(store)))
    app = create_app(provider, store, AlertStateStore(":memory:"))
    ai = AITelemetry(provider, app.state.metrics)

    @app.get("/users/{email}")
    def user(email: str):
        with get_tracer(provider).start_as_current_span("db.lookup") as sp:
            sp.set_attribute("db.statement", f"SELECT * FROM users WHERE email='{email}' AND api_key=hunter2-canary")
            sp.set_attribute("http.url", "http://x/y?token=CANARYabcdefghijklmnopqrst")
            sp.set_attribute("http.request.header.authorization", "Bearer CANARYTOKEN123456789")
        with ai.operation(OperationKind.MODEL_CALL, provider="p", target=f"model-{email}") as op:
            op.set_usage(input_tokens=3, output_tokens=2)
        raise RuntimeError(f"upstream failed for {email}: password=hunter2-canary key=sk-CANARYabcdefghijklmnopqrst "
                           f"card 4111 1111 1111 1111")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/users/canary.user@example.com?token=CANARYabcdefghijklmnopqrst",
                      headers={"Authorization": "Bearer CANARYTOKEN123456789", "Cookie": "session=hunter2-canary"})
    assert resp.status_code == 500 and "RuntimeError" not in resp.text

    everything = [capsys.readouterr().out, client.get("/metrics").text]
    for path in ("/api/overview", "/api/errors", "/api/traces", "/api/costs/summary"):
        everything.append(client.get(path).text)
    trace_id = client.get("/api/traces").json()["traces"][0]["trace_id"]
    everything.append(client.get(f"/api/traces/{trace_id}").text)
    everything.append(json.dumps([tuple(map(str, r)) for r in store._db().execute("SELECT * FROM spans")]))
    for sp in exporter.get_finished_spans():
        everything.append(json.dumps(dict(sp.attributes), default=str))
        everything.append(json.dumps([(e.name, dict(e.attributes or {})) for e in sp.events], default=str))
        everything.append(sp.name + str(sp.status.description))
    blob = "\n".join(everything)

    for canary in CANARIES:
        assert canary.lower() not in blob.lower(), f"leaked: {canary}"
    # Not vacuous: the failure itself IS recorded, just without the secrets.
    assert "RuntimeError" in blob and "/users/{email}" in blob and "[redacted" in blob


def test_exception_event_has_type_and_redacted_message_but_no_stack_trace():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(exporter)))
    app = create_app(provider, TraceStore(":memory:"), AlertStateStore(":memory:"))

    @app.get("/boom")
    def boom():
        raise ValueError("bad password=hunter2-canary")

    TestClient(app, raise_server_exceptions=False).get("/boom")
    span = next(s for s in exporter.get_finished_spans() if s.name == "GET /boom")
    (event,) = [e for e in span.events if e.name == "exception"]
    assert event.attributes["exception.type"] == "ValueError"
    assert "hunter2" not in event.attributes["exception.message"]
    assert "exception.stacktrace" not in event.attributes


# ---------------- bounded cardinality ----------------
def _samples(text):
    return [s for f in text_string_to_metric_families(text) for s in f.samples]


def test_thousand_distinct_unknown_paths_make_one_series(monkeypatch):
    c = _client(monkeypatch)
    for i in range(1000):
        c.get(f"/scanner/{i}/x")
    series = {tuple(sorted(s.labels.items())) for s in _samples(c.get("/metrics").text)
              if s.name == "obsdash_http_requests_total"}
    assert len(series) == 1


def test_arbitrary_http_methods_collapse_to_other(monkeypatch):
    c = _client(monkeypatch)
    for m in ("FOO", "BAR", "PURGE", "X-ATTACK-1"):
        c.request(m, "/health")
    methods = {s.labels["method"] for s in _samples(c.get("/metrics").text) if s.name == "obsdash_http_requests_total"}
    assert methods <= {"GET", "OTHER"} and "OTHER" in methods


def test_ai_targets_are_capped_and_overflow_is_counted():
    from obsdash.metrics import Metrics
    metrics = Metrics()
    ai = AITelemetry(TracerProvider(), metrics)
    for i in range(500):
        with ai.operation(OperationKind.MODEL_CALL, provider="p", target=f"user-supplied-model-{i}"):
            pass
    s = _samples(metrics.render()[0].decode())
    targets = {x.labels["target"] for x in s if x.name == "obsdash_ai_operations_total"}
    assert len(targets) == MAX_DISTINCT_TARGETS + 1 and OTHER in targets
    overflow = next(x.value for x in s if x.name == "obsdash_label_overflow_total")
    assert overflow == 500 - MAX_DISTINCT_TARGETS


def test_hostile_request_ids_are_replaced_not_echoed(monkeypatch):
    c = _client(monkeypatch)
    for bad in ("x" * 500, "has space", "<script>alert(1)</script>", "semi;colon", "üñí"):
        try:
            r = c.get("/health", headers={"X-Request-ID": bad})
        except Exception:  # some clients refuse non-ASCII headers outright; that's also safe
            continue
        assert r.headers["X-Request-ID"] != bad and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", r.headers["X-Request-ID"])
    assert c.get("/health", headers={"X-Request-ID": "good-id_1.2"}).headers["X-Request-ID"] == "good-id_1.2"


# ---------------- retention / limits / timeouts ----------------
def _old_and_new_spans(store, now_ns, hours_old):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(store)))
    tracer = get_tracer(provider)
    old = now_ns - int(hours_old * 3600 * 1e9)
    sp = tracer.start_span("old-span", start_time=old)
    sp.end(end_time=old + 1_000_000)
    store._last_age_prune = 0.0       # allow the (rate-limited) age prune to run on the next write
    sp = tracer.start_span("new-span", start_time=now_ns - 1_000_000)
    sp.end(end_time=now_ns)


def test_age_based_retention_deletes_old_spans():
    now = time.time_ns()
    store = TraceStore(":memory:", retention_hours=24, clock=lambda: now)
    _old_and_new_spans(store, now, hours_old=30)
    assert {t["root_name"] for t in store.list_traces()} == {"new-span"}


def test_spans_inside_retention_are_kept():
    now = time.time_ns()
    store = TraceStore(":memory:", retention_hours=24, clock=lambda: now)
    _old_and_new_spans(store, now, hours_old=2)
    assert {t["root_name"] for t in store.list_traces()} == {"old-span", "new-span"}


def test_file_database_uses_wal(tmp_path):
    store = TraceStore(str(tmp_path / "t.db"))
    assert store._db().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    store.close()


def _fill(store, n):
    rows = [(f"{i:032x}", f"{i:016x}", None, f"GET /r{i % 50}", "SERVER", "svc", i, i + 1000, "OK", "{}", None)
            for i in range(n)]
    store._db().executemany("INSERT INTO spans (trace_id, span_id, parent_id, name, kind, service, start_ns, "
                            "end_ns, status, attributes, status_message) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)


def test_slow_query_is_aborted_and_store_recovers():
    store = TraceStore(":memory:", query_timeout_s=0.0)
    _fill(store, 20_000)
    with pytest.raises(QueryTimeout):
        store.cost_summary()
    store._query_timeout_s = 5.0            # the store is not left wedged
    assert store.span_count() == 20_000 and store.cost_summary()["totals"]["calls"] == 0


def test_api_returns_503_on_query_timeout():
    store = TraceStore(":memory:", query_timeout_s=0.0)
    _fill(store, 20_000)
    c = TestClient(create_app(TracerProvider(), store, AlertStateStore(":memory:")), raise_server_exceptions=False)
    r = c.get("/api/overview")
    assert r.status_code == 503 and r.json() == {"error": "query_timeout"} and r.headers["retry-after"] == "5"


# ---------------- exporter failures + non-blocking export ----------------
def test_store_failure_is_counted_and_never_breaks_requests():
    from obsdash.metrics import Metrics

    class BrokenStore:
        def add_spans(self, spans):
            raise RuntimeError("disk full")

    metrics = Metrics()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(BrokenStore(), metrics.trace_export_failures_total.inc)))
    app = create_app(provider, TraceStore(":memory:"), AlertStateStore(":memory:"))
    assert TestClient(app).get("/health").status_code == 200      # the request is unaffected
    s = _samples(metrics.render()[0].decode())
    assert next(x.value for x in s if x.name == "obsdash_trace_export_failures_total") >= 1


def test_slow_trace_store_does_not_block_requests():
    class SlowStore(TraceStore):
        def add_spans(self, spans):
            time.sleep(1.0)

    store = SlowStore(":memory:")
    provider = build_tracer_provider(Settings(), store)        # the REAL production wiring (BatchSpanProcessor)
    app = create_app(provider, store, AlertStateStore(":memory:"))
    client = TestClient(app)
    t = time.perf_counter()
    for _ in range(40):
        assert client.get("/health").status_code == 200
    elapsed = time.perf_counter() - t
    assert elapsed < 0.8, f"requests were slowed by a slow exporter ({elapsed:.2f}s)"
    provider.shutdown()


def test_otlp_endpoint_credentials_are_not_logged():
    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(str(record.__dict__))

    log = logging.getLogger("obsdash.otel_setup")
    h = H()
    log.addHandler(h)
    log.setLevel(logging.INFO)
    try:
        p = build_tracer_provider(Settings(otel_exporter_otlp_endpoint="http://user:SECRETPASS@collector:4318/v1/traces"))
        p.shutdown()
    finally:
        log.removeHandler(h)
    blob = " ".join(seen)
    assert "SECRETPASS" not in blob and "collector" in blob


def test_every_production_exporter_is_wrapped_in_redaction():
    """Console, OTLP and the local store must all pass through RedactingSpanExporter,
    otherwise a raw secret set on a span would be exported untouched."""
    for endpoint in ("", "http://collector:4318/v1/traces"):
        store = TraceStore(":memory:")
        provider = build_tracer_provider(Settings(otel_exporter_otlp_endpoint=endpoint), store)
        procs = provider._active_span_processor._span_processors
        assert len(procs) == 2
        assert all(isinstance(p.span_exporter, RedactingSpanExporter) for p in procs)
        provider.shutdown()


def test_raw_instrumentation_secrets_are_scrubbed_before_export():
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(inner)))
    with get_tracer(provider).start_as_current_span("job for a@b.com") as sp:
        sp.set_attribute("db.statement", "UPDATE t SET api_key=hunter2")
        sp.set_attribute("headers.authorization", "Bearer abcdefghijklmnop")
        sp.set_attribute("tags", ["ok", "password=hunter2"])
        sp.set_attribute("count", 7)
        sp.add_event("note", {"msg": "token: sk-ABCDEFGHIJKLMNOPQRSTUVWX"})
    (span,) = inner.get_finished_spans()
    blob = json.dumps([span.name, dict(span.attributes), [dict(e.attributes) for e in span.events]], default=str)
    assert "hunter2" not in blob and "a@b.com" not in blob and "abcdefghijklmnop" not in blob and "sk-ABCDEF" not in blob
    assert span.attributes["count"] == 7  # types preserved
