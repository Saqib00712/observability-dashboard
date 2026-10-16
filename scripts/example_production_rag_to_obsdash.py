"""
Phase 11 example: send REAL production-rag spans to obsdash, with no change
to production-rag. This runs production-rag's own `init_otel` + `span()` for
the same stage names its pipeline uses (no OpenAI calls).

  1) python -m uvicorn obsdash.main:app --port 8100            (terminal A)
  2) python scripts/example_production_rag_to_obsdash.py --production-rag ../production-rag
  3) dashboard -> "AI & cost": retrieval / rerank / model_call / embedding rows
     (cost shows as UNPRICED: production-rag spans carry no token counts)

To observe the REAL app instead, start it with (no code change):
  OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8100/v1/traces
  [OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <token>"  if obsdash has API_TOKEN]
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, so `obsdash` imports
import time

ap = argparse.ArgumentParser()
ap.add_argument("--production-rag", required=True)
ap.add_argument("--endpoint", default="http://127.0.0.1:8100/v1/traces")
ap.add_argument("--token", default="")
args = ap.parse_args()
if args.token:
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Bearer {args.token}"
os.chdir(args.production_rag)
sys.path.insert(0, args.production_rag)

from app.config import Settings  # noqa: E402
from app.observability.otel_export import init_otel  # noqa: E402
from app.observability.tracing import span, start_trace  # noqa: E402

init_otel(Settings(otel_enabled=True, otel_exporter_otlp_endpoint=args.endpoint))
for i in range(3):
    start_trace(f"demo-{i}")
    with span("retrieval", document_id="demo-doc"):
        time.sleep(0.01)
        with span("rerank", candidates=5):
            time.sleep(0.005)
    with span("generation", sources=3):
        time.sleep(0.03)
print("spans queued; they are flushed when this process exits")
