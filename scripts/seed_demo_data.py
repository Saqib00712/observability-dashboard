"""
Generate SYNTHETIC telemetry for demoing the dashboard.

This is NOT a hardcoded dashboard: it pushes simulated traffic (a fake RAG
app: embed -> retrieve -> rerank -> LLM, with random slow and failing
requests) through obsdash's REAL instrumentation pipeline into a SQLite
file. The dashboard then reads that file exactly as it would read real
telemetry. Timestamps are shifted back so the data spans the last ~55
minutes instead of one second.

Usage (from the repo root):
    python scripts/seed_demo_data.py demo.db 200
    set SQLITE_PATH=demo.db          (PowerShell: $env:SQLITE_PATH="demo.db")
    python -m uvicorn obsdash.main:app --port 8100
    open http://localhost:8100/dashboard
"""

import os
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402

from obsdash.ai_telemetry import AITelemetry, OperationKind  # noqa: E402
from obsdash.main import create_app  # noqa: E402
from obsdash.pricing import PricingTable  # noqa: E402
from obsdash.trace_store import TraceStore, TraceStoreExporter  # noqa: E402


def main(db_path: str, n_requests: int, seed: int = 7) -> None:
    rnd = random.Random(seed)
    if os.path.exists(db_path):
        os.remove(db_path)
    store = TraceStore(db_path)
    provider = TracerProvider(resource=Resource.create({"service.name": "obsdash-demo"}))
    provider.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(store)))
    app = create_app(provider, store)
    root = Path(__file__).resolve().parent.parent
    # Repo checkout has it at the root; the Docker image ships it under config/.
    pricing_file = next(p for p in (root / "pricing.example.json", root / "config" / "pricing.example.json") if p.exists())
    pricing = PricingTable.from_file(str(pricing_file))
    ai = AITelemetry(provider, app.state.metrics, pricing)
    app.state.ai = ai

    def nap(lo_ms: float, hi_ms: float) -> None:
        time.sleep(rnd.uniform(lo_ms, hi_ms) / 1000)

    @app.get("/ask")
    def ask():
        with ai.operation(OperationKind.AGENT_STEP, provider="local", target="rag-agent"):
            with ai.operation(OperationKind.EMBEDDING, provider="fake", target="fake-embed") as op:
                nap(2, 8); op.set_usage(input_tokens=rnd.randint(8, 40))
            with ai.operation(OperationKind.RETRIEVAL, provider="fake", target="vector-index") as op:
                nap(3, 15); op.set_result_count(rnd.randint(3, 8))
            with ai.operation(OperationKind.RERANK, provider="fake", target="fake-rerank") as op:
                nap(2, 10); op.set_result_count(3)
            # Mostly the priced model; sometimes a model missing from the pricing file.
            model = "fake-llm" if rnd.random() > 0.12 else "fake-mystery"
            with ai.operation(OperationKind.MODEL_CALL, provider="fake", target=model) as op:
                slow = rnd.random() < 0.06
                nap(150, 300) if slow else nap(10, 60)
                if rnd.random() < 0.08:
                    raise TimeoutError("simulated provider timeout")
                if rnd.random() < 0.05:
                    pass  # provider reported no usage: cost must stay unknown
                else:
                    op.set_usage(input_tokens=rnd.randint(300, 1500), output_tokens=rnd.randint(50, 400))
        return {"answer": "synthetic"}

    @app.get("/items/{item_id}")
    def item(item_id: int):
        nap(1, 6)
        return {"id": item_id}

    client = TestClient(app, raise_server_exceptions=False)
    for _ in range(n_requests):
        r = rnd.random()
        if r < 0.65:
            client.get("/ask")
        elif r < 0.9:
            client.get(f"/items/{rnd.randint(1, 9999)}")
        elif r < 0.95:
            client.get("/does-not-exist")  # 404: client error, not a service error
        else:
            client.get("/health")
    provider.shutdown()

    # Spread the traces across the last ~55 minutes (shift whole traces so
    # parent/child structure stays intact).
    con = sqlite3.connect(db_path)
    now_ns = time.time_ns()
    trace_ids = [r[0] for r in con.execute("SELECT DISTINCT trace_id FROM spans")]
    for tid in trace_ids:
        back_ns = int(rnd.uniform(0, 55 * 60) * 1e9)
        con.execute("UPDATE spans SET start_ns = start_ns - ?, end_ns = end_ns - ? WHERE trace_id = ?",
                    (back_ns, back_ns, tid))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    con.close()
    print(f"seeded {n} spans in {len(trace_ids)} traces -> {db_path} (SYNTHETIC)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo.db", int(sys.argv[2]) if len(sys.argv) > 2 else 200)
