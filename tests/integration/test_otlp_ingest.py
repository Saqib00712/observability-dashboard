"""Phase 11: the OTLP/HTTP receiver, exercised with hand-built protobuf
requests (wire-level behaviour) via the in-process test client."""

import gzip
import json

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as pb
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from obsdash.config import get_settings
from obsdash.main import create_app
from obsdash.trace_store import TraceStore, TraceStoreExporter

PB = {"content-type": "application/x-protobuf"}
NOW = 1_800_000_000_000_000_000


def _span(rs, name="work", trace=1, span=1, start=NOW, end=NOW + 5_000_000, kind=1, status=0, attrs=None, parent=None):
    s = rs.scope_spans.add().spans.add() if not rs.scope_spans else rs.scope_spans[0].spans.add()
    s.trace_id = trace.to_bytes(16, "big")
    s.span_id = span.to_bytes(8, "big")
    if parent:
        s.parent_span_id = parent.to_bytes(8, "big")
    s.name, s.kind, s.start_time_unix_nano, s.end_time_unix_nano = name, kind, start, end
    s.status.code = status
    for k, v in (attrs or {}).items():
        kv = s.attributes.add()
        kv.key = k
        if isinstance(v, bool):
            kv.value.bool_value = v
        elif isinstance(v, int):
            kv.value.int_value = v
        elif isinstance(v, float):
            kv.value.double_value = v
        else:
            kv.value.string_value = str(v)
    return s


def _request(service="svc", spans=()):
    req = pb.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    a = rs.resource.attributes.add()
    a.key, a.value.string_value = "service.name", service
    for kw in spans:
        _span(rs, **kw)
    return req


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    get_settings.cache_clear()
    store = TraceStore(str(tmp_path / "t.db"))
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(store)))
    app = create_app(tracer_provider=provider, trace_store=store)
    yield TestClient(app), store, app
    store.close()
    get_settings.cache_clear()


def _rows(store):
    return store.query_spans(kinds=("SERVER", "CLIENT", "INTERNAL", "PRODUCER", "CONSUMER"))


def test_valid_spans_are_stored_and_response_is_protobuf(env):
    client, store, _ = env
    body = _request("svc", [dict(name="a", span=1), dict(name="b", span=2, parent=1, status=2)]).SerializeToString()
    r = client.post("/v1/traces", content=body, headers=PB)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-protobuf")
    resp = pb.ExportTraceServiceResponse()
    resp.ParseFromString(r.content)
    assert resp.partial_success.rejected_spans == 0
    got = {x["name"]: x for x in _rows(store)}
    assert set(got) == {"a", "b"}
    assert got["b"]["status"] == "ERROR" and got["a"]["service"] == "svc"
    assert got["b"]["parent_id"] == format(1, "016x")


def test_gzip_body_accepted(env):
    client, store, _ = env
    body = gzip.compress(_request(spans=[dict(name="z")]).SerializeToString())
    r = client.post("/v1/traces", content=body, headers={**PB, "content-encoding": "gzip"})
    assert r.status_code == 200 and [x["name"] for x in _rows(store)] == ["z"]


def test_json_and_unknown_encoding_are_415(env):
    client, _, _ = env
    assert client.post("/v1/traces", content=b"{}", headers={"content-type": "application/json"}).status_code == 415
    assert client.post("/v1/traces", content=b"x", headers={**PB, "content-encoding": "br"}).status_code == 415


def test_garbage_protobuf_and_garbage_gzip_are_400(env):
    client, store, _ = env
    assert client.post("/v1/traces", content=b"\xff\xff\xff not protobuf", headers=PB).status_code == 400
    assert client.post("/v1/traces", content=b"nope", headers={**PB, "content-encoding": "gzip"}).status_code == 400
    assert store.span_count() == 0


def test_body_too_large_and_gzip_bomb_are_413(env, monkeypatch):
    client, store, app = env
    app.state.otlp_max_body_bytes = 2048
    assert client.post("/v1/traces", content=b"a" * 5000, headers=PB).status_code == 413
    bomb = gzip.compress(b"\0" * 1_000_000)  # tiny on the wire, huge when expanded
    assert len(bomb) < 2048
    assert client.post("/v1/traces", content=bomb, headers={**PB, "content-encoding": "gzip"}).status_code == 413
    assert store.span_count() == 0


def test_too_many_spans_is_413(env):
    client, store, app = env
    app.state.otlp_max_spans_per_request = 3
    body = _request(spans=[dict(span=i + 1) for i in range(4)]).SerializeToString()
    assert client.post("/v1/traces", content=body, headers=PB).status_code == 413
    assert store.span_count() == 0


def test_invalid_spans_are_dropped_and_reported_as_partial_success(env):
    client, store, _ = env
    req = _request(spans=[dict(name="good", span=1), dict(name="zero-start", span=2, start=0)])
    bad = req.resource_spans[0].scope_spans[0].spans.add()  # no ids at all
    bad.name = "no-ids"
    r = client.post("/v1/traces", content=req.SerializeToString(), headers=PB)
    resp = pb.ExportTraceServiceResponse()
    resp.ParseFromString(r.content)
    assert r.status_code == 200 and resp.partial_success.rejected_spans == 2
    assert [x["name"] for x in _rows(store)] == ["good"]
    m = client.get("/metrics").text
    assert 'obsdash_otlp_spans_total{result="rejected"} 2.0' in m
    assert 'obsdash_otlp_requests_total{outcome="partial"} 1.0' in m


def test_backwards_end_time_is_clamped(env):
    client, store, _ = env
    client.post("/v1/traces", content=_request(spans=[dict(start=NOW, end=NOW - 10)]).SerializeToString(), headers=PB)
    (row,) = _rows(store)
    assert row["end_ns"] == row["start_ns"]


def test_secrets_in_ingested_spans_are_redacted(env):
    client, store, _ = env
    secret = "sk-" + "a1B2c3D4" * 5
    body = _request(spans=[dict(name="s", attrs={"api_key": "hunter2", "note": f"key {secret}", "ok": 7})]).SerializeToString()
    client.post("/v1/traces", content=body, headers=PB)
    (row,) = _rows(store)
    blob = row["attributes"]
    assert "hunter2" not in blob and secret not in blob
    assert json.loads(blob)["ok"] == 7


def test_receiving_telemetry_does_not_create_telemetry_about_itself(env):
    client, store, _ = env
    for _i in range(3):
        client.post("/v1/traces", content=_request(spans=[dict(name="x", span=_i + 1)]).SerializeToString(), headers=PB)
    names = {x["name"] for x in _rows(store)}
    assert names == {"x"}  # no "POST /v1/traces" spans


def test_token_is_required_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "t" * 20)
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "a.db"))
    get_settings.cache_clear()
    try:
        store = TraceStore(str(tmp_path / "a.db"))
        client = TestClient(create_app(trace_store=store))
        body = _request(spans=[dict(name="x")]).SerializeToString()
        assert client.post("/v1/traces", content=body, headers=PB).status_code == 401
        assert client.post("/v1/traces", content=body, headers={**PB, "x-api-key": "wrong" * 5}).status_code == 401
        assert store.span_count() == 0
        ok = client.post("/v1/traces", content=body, headers={**PB, "authorization": "Bearer " + "t" * 20})
        assert ok.status_code == 200 and store.span_count() == 1
    finally:
        get_settings.cache_clear()


def test_storage_failure_answers_503_so_exporters_retry(env):
    client, store, app = env

    def boom(rows):
        raise RuntimeError("disk full")

    app.state.trace_store = type("Broken", (), {"add_rows": staticmethod(boom)})()
    r = client.post("/v1/traces", content=_request(spans=[dict()]).SerializeToString(), headers=PB)
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert "disk full" not in r.text


def test_production_rag_stage_spans_are_classified_without_inventing_data(env):
    client, store, _ = env
    spans = [dict(name=n, span=i + 1, attrs={"chunks": 3}) for i, n in enumerate(
        ["retrieval", "rerank", "generation", "embedding", "contextualize"])]
    client.post("/v1/traces", content=_request("production-rag", spans).SerializeToString(), headers=PB)
    rows = {x["name"]: x for x in _rows(store)}
    ctx = rows["contextualize"]  # not a billed AI stage: stored, but left as an ordinary span
    assert ctx["kind"] == "INTERNAL" and "gen_ai" not in ctx["attributes"]
    gen = json.loads(rows["generation"]["attributes"])
    assert rows["generation"]["kind"] == "CLIENT"
    assert gen["gen_ai.operation.name"] == "model_call"
    assert gen["obsdash.ai.cost_status"] == "no_usage"       # unknown, not free
    assert "gen_ai.usage.input_tokens" not in gen and "obsdash.ai.cost_amount" not in gen
    assert gen["obsdash.enriched_by"] == "production_rag_adapter" and gen["chunks"] == 3
    assert json.loads(rows["retrieval"]["attributes"])["gen_ai.operation.name"] == "retrieval"
    assert "obsdash.ai.cost_status" not in json.loads(rows["retrieval"]["attributes"])


def test_other_services_and_self_declared_spans_are_not_reclassified(env):
    client, store, _ = env
    client.post("/v1/traces", content=_request("other-app", [dict(name="generation", span=1)]).SerializeToString(), headers=PB)
    client.post("/v1/traces", content=_request("production-rag", [dict(
        name="generation", span=2, kind=3, attrs={"gen_ai.operation.name": "chat"})]).SerializeToString(), headers=PB)
    rows = sorted(_rows(store), key=lambda r: r["span_id"])
    assert rows[0]["kind"] == "INTERNAL" and "gen_ai" not in rows[0]["attributes"]
    assert json.loads(rows[1]["attributes"])["gen_ai.operation.name"] == "chat"
    assert "enriched_by" not in rows[1]["attributes"]


def test_enrichment_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OTLP_ENRICH_KNOWN_SERVICES", "false")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "d.db"))
    get_settings.cache_clear()
    try:
        store = TraceStore(str(tmp_path / "d.db"))
        client = TestClient(create_app(trace_store=store))
        client.post("/v1/traces", content=_request("production-rag", [dict(name="generation")]).SerializeToString(), headers=PB)
        (row,) = _rows(store)
        assert row["kind"] == "INTERNAL" and "gen_ai" not in row["attributes"]
    finally:
        get_settings.cache_clear()
