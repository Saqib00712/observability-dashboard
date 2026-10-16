"""Phase 13: the deployment files can't be run in CI without a Docker daemon,
so (a) the smoke-test script is exercised against a live server, and
(b) the security-relevant lines of the Docker files are pinned as text checks
so a later edit can't silently weaken them."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_smoke_script_passes_against_a_live_server(live_server):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_test.py"), live_server["url"]],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "6/6 checks passed" in r.stdout


def test_smoke_script_fails_loudly_when_nothing_is_listening():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_test.py"), "http://127.0.0.1:9"],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 1 and "FAIL" in r.stdout


def test_compose_publishes_to_host_loopback_only_and_hardens_the_container():
    c = (ROOT / "docker-compose.yml").read_text()
    assert '"127.0.0.1:${HOST_PORT:-8100}:8100"' in c      # never 0.0.0.0 on the host
    assert "read_only: true" in c and "cap_drop: [ALL]" in c and "no-new-privileges:true" in c
    assert "obsdash-data:/data" in c                        # data survives container re-creation


def test_dockerfile_runs_non_root_with_healthcheck_and_no_baked_secrets():
    d = (ROOT / "Dockerfile").read_text()
    assert "USER obsdash" in d and "HEALTHCHECK" in d
    for forbidden in ("API_TOKEN", "ALERT_WEBHOOK_URL", "COPY .env", "COPY . "):
        assert forbidden not in d


def test_dockerignore_keeps_secrets_and_data_out_of_the_image():
    ig = (ROOT / ".dockerignore").read_text().split()
    assert {".env", "*.db", "*.db-wal", "tests"} <= set(ig)


def test_env_example_contains_no_real_secret():
    e = (ROOT / ".env.example").read_text()
    assert "API_TOKEN=\n" in e and "ALERT_WEBHOOK_URL=\n" in e


def test_container_healthcheck_uses_the_untracked_probe_endpoint():
    for f in ("Dockerfile", "docker-compose.yml"):
        text = (ROOT / f).read_text()
        assert "8100/healthz" in text and "8100/health'" not in text


def test_seed_script_runs_and_fills_a_database(tmp_path):
    """The compose `seed` service runs this inside the runtime image, so it must work with runtime
    requirements only (it once failed in Docker for want of httpx)."""
    import sqlite3
    db = tmp_path / "seed.db"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_demo_data.py"), str(db), "20"],
                       capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stdout[-500:] + r.stderr[-1500:]
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM spans").fetchone()[0] > 20


def test_runtime_requirements_cover_what_the_shipped_scripts_import():
    req = (ROOT / "requirements.txt").read_text()
    assert "httpx==" in req  # scripts/seed_demo_data.py and demo_alerts.py use the FastAPI test client


def test_seed_script_works_from_the_docker_image_layout(tmp_path):
    """Replicates what the Dockerfile copies (obsdash/, scripts/, config/ -- NOT the repo-root example files)."""
    import shutil
    import sqlite3
    img = tmp_path / "image"
    img.mkdir()
    for name in ("obsdash", "scripts", "config"):
        shutil.copytree(ROOT / name, img / name, ignore=shutil.ignore_patterns("__pycache__"))
    db = tmp_path / "img.db"
    r = subprocess.run([sys.executable, str(img / "scripts" / "seed_demo_data.py"), str(db), "20"],
                       capture_output=True, text=True, timeout=120, cwd=img)
    assert r.returncode == 0, r.stdout[-500:] + r.stderr[-1500:]
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM spans").fetchone()[0] > 20
