"""
Phase 11 example: run a Project 2 (eval-harness) evaluation and watch it in
obsdash. Uses eval-harness's own MockSystem -- no paid AI calls.

  1) python -m uvicorn obsdash.main:app --port 8100            (terminal A)
  2) python scripts/example_eval_to_obsdash.py --eval-harness ../eval-harness
       [--endpoint http://127.0.0.1:8100/v1/traces] [--token YOUR_API_TOKEN]
  3) open http://127.0.0.1:8100/dashboard -> Traces tab (service "eval-harness")
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, so `obsdash` imports

ap = argparse.ArgumentParser()
ap.add_argument("--eval-harness", required=True, help="path to the eval-harness repo")
ap.add_argument("--endpoint", default="http://127.0.0.1:8100/v1/traces")
ap.add_argument("--token", default="")
args = ap.parse_args()
sys.path.insert(0, args.eval_harness)

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402

from eval_harness.dataset import Dataset  # noqa: E402
from eval_harness.evaluators import ExactMatchEvaluator  # noqa: E402
from eval_harness.mock_system import MockSystem  # noqa: E402
from eval_harness.models import TestCase  # noqa: E402
from eval_harness.runner import run_evaluation  # noqa: E402
from eval_harness.system_output import SystemOutput  # noqa: E402

from obsdash.integrations.eval_harness_observer import ObsdashRunObserver  # noqa: E402

headers = {"Authorization": f"Bearer {args.token}"} if args.token else None
provider = TracerProvider(resource=Resource.create({"service.name": "eval-harness"}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=args.endpoint, headers=headers, timeout=5)))

cases = [TestCase(test_id=f"qa-{i}", input="q", expected_output="Paris") for i in range(5)]
system = MockSystem({c.test_id: SystemOutput(output="Paris" if i % 2 == 0 else "Lyon", latency_ms=10.0 + i)
                     for i, c in enumerate(cases)}, name="demo-mock")
run = asyncio.run(run_evaluation(
    Dataset(dataset_id="demo", name="demo", version="1", test_cases=cases), system, [ExactMatchEvaluator()],
    observer=ObsdashRunObserver(provider, system_name=system.name)))
provider.shutdown()
print(f"run {run.run_id}: {run.passed_tests}/{run.total_tests} passed; spans sent to {args.endpoint}")
