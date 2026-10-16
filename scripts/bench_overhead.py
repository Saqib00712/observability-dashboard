"""
Rough per-request overhead of obsdash instrumentation (middleware + span +
metrics + structured log lines + background span export) versus a bare
FastAPI app serving the same handler.

INDICATIVE ONLY: measured through Starlette's TestClient (in-process, no
network, no real server), on whatever machine runs it. Use it to compare
before/after changes, not as a production latency claim.

    python scripts/bench_overhead.py [requests_per_round]
"""

import io
import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: E402

from obsdash.alert_engine import AlertStateStore  # noqa: E402
from obsdash.main import create_app  # noqa: E402
from obsdash.span_redaction import RedactingSpanExporter  # noqa: E402
from obsdash.trace_store import TraceStore, TraceStoreExporter  # noqa: E402


def per_request_ms(client: TestClient, n: int) -> float:
    for _ in range(50):  # warm-up
        client.get("/ping")
    t = time.perf_counter()
    for _ in range(n):
        client.get("/ping")
    return (time.perf_counter() - t) / n * 1000


def main(n: int) -> None:
    bare = FastAPI()

    @bare.get("/ping")
    def ping_bare():
        return {"ok": True}

    store = TraceStore(":memory:")
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(TraceStoreExporter(store)),
                                                   max_queue_size=2048, max_export_batch_size=512,
                                                   schedule_delay_millis=1000))
    app = create_app(provider, store, AlertStateStore(":memory:"))

    @app.get("/ping")
    def ping_instrumented():
        return {"ok": True}

    # Keep the JSON log lines out of the timing (they go to a throwaway buffer).
    logging.getLogger().handlers[0].stream = io.StringIO()

    c_bare, c_inst = TestClient(bare), TestClient(app)
    bare_ms, inst_ms = [], []
    for _ in range(5):  # interleave rounds so drift hits both equally
        bare_ms.append(per_request_ms(c_bare, n))
        inst_ms.append(per_request_ms(c_inst, n))
    b, i = statistics.median(bare_ms), statistics.median(inst_ms)
    print(f"requests per round: {n}, rounds: 5 (median shown)")
    print(f"bare FastAPI:        {b:.3f} ms/request")
    print(f"obsdash instrumented:{i:.3f} ms/request")
    print(f"overhead:            {i - b:.3f} ms/request ({(i / b - 1) * 100:.0f}% of this tiny handler)")
    provider.shutdown()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000)
