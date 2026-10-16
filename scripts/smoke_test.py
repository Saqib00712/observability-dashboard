"""
Post-deployment smoke test. Works against ANY running obsdash (local or Docker).
Sends one real OTLP trace, then checks every surface noticed it.

    python scripts/smoke_test.py http://127.0.0.1:8100 [--token YOUR_API_TOKEN]

Exit code 0 = all checks passed. Prints one PASS/FAIL line per check.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ap = argparse.ArgumentParser()
ap.add_argument("base", nargs="?", default="http://127.0.0.1:8100")
ap.add_argument("--token", default="")
args = ap.parse_args()
base = args.base.rstrip("/")
auth = {"Authorization": f"Bearer {args.token}"} if args.token else {}
results: list[tuple[str, bool, str]] = []


def get(path, headers=None):
    req = urllib.request.Request(base + path, headers=headers if headers is not None else auth)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   ({detail})" if detail else ""))


try:
    st, _, body = get("/health", {})
    check("health endpoint answers 200 without a token", st == 200 and json.loads(body)["status"] == "ok", f"HTTP {st}")
except Exception as exc:  # noqa: BLE001
    check("health endpoint reachable", False, f"{type(exc).__name__}: cannot connect to {base}")
    sys.exit(1)

st, hdr, _ = get("/dashboard", {})
csp = {k.lower(): v for k, v in hdr.items()}.get("content-security-policy", "")
check("dashboard served with strict CSP", st == 200 and "default-src 'none'" in csp and "unsafe-inline" not in csp)

st, _, _ = get("/api/overview", {})
if args.token:
    check("API rejects a request with no token", st == 401, f"HTTP {st}")
else:
    check("API open (no token configured)", st == 200, f"HTTP {st}")

# --- send a real OTLP trace with the stock exporter ---
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402

marker = f"smoke-{int(time.time())}"
provider = TracerProvider(resource=Resource.create({"service.name": "obsdash-smoke-test"}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=base + "/v1/traces", headers=auth or None, timeout=5)))
tracer = provider.get_tracer("smoke")
with tracer.start_as_current_span(marker):
    with tracer.start_as_current_span("child-step"):
        pass
provider.shutdown()

found = None
for _ in range(20):
    st, _, body = get("/api/traces?limit=50")
    if st == 200:
        found = next((t for t in json.loads(body)["traces"] if t["root_name"] == marker), None)
        if found:
            break
    time.sleep(0.5)
check("OTLP trace accepted and visible in /api/traces", found and found["span_count"] == 2,
      f"HTTP {st}" if not found else f"trace {found['trace_id'][:8]}...")

if args.token:
    bad = urllib.request.Request(base + "/v1/traces", data=b"", method="POST",
                                 headers={"Content-Type": "application/x-protobuf"})
    try:
        urllib.request.urlopen(bad, timeout=5)
        check("ingest rejects a request with no token", False, "got 2xx")
    except urllib.error.HTTPError as e:
        check("ingest rejects a request with no token", e.code == 401, f"HTTP {e.code}")

st, _, body = get("/metrics")
text = body.decode() if st == 200 else ""
check("/metrics exposes OTLP counters", "obsdash_otlp_spans_total" in text, f"HTTP {st}")

st, _, body = get("/api/alerts")
check("alerts endpoint answers", st == 200, f"HTTP {st}")

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
