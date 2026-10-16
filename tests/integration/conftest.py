"""Phase 11 integration fixtures. Sibling projects are OPTIONAL: tests that
need them skip cleanly when they are not found next to this repo (or at
OBSDASH_EVAL_HARNESS_PATH / OBSDASH_PRODUCTION_RAG_PATH)."""

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from obsdash.config import get_settings
from obsdash.main import create_app
from obsdash.trace_store import TraceStore

_ROOT = Path(__file__).resolve().parents[2]


def sibling(env_name: str, dirname: str) -> Path | None:
    for cand in (os.environ.get(env_name), str(_ROOT.parent / dirname)):
        if cand and Path(cand).is_dir():
            return Path(cand)
    return None


EVAL_HARNESS = sibling("OBSDASH_EVAL_HARNESS_PATH", "eval-harness")
PRODUCTION_RAG = sibling("OBSDASH_PRODUCTION_RAG_PATH", "production-rag")
if EVAL_HARNESS and str(EVAL_HARNESS) not in sys.path:
    sys.path.append(str(EVAL_HARNESS))


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """A real obsdash on a real socket (so a REAL OTLP exporter can talk to it)."""
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "live.db"))
    monkeypatch.setenv("SERVICE_NAME", "obsdash")
    get_settings.cache_clear()
    store = TraceStore(str(tmp_path / "live.db"))
    app = create_app(trace_store=store)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "live obsdash did not start"
    yield {"url": f"http://127.0.0.1:{port}", "store": store, "app": app}
    server.should_exit = True
    t.join(timeout=10)
    get_settings.cache_clear()
