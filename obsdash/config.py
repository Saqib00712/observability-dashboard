"""
Phase 1: configuration, via `pydantic-settings` -- the same env-driven
pattern `production-rag/app/config.py` already uses (a `Settings`
`BaseSettings` subclass, one instance built at import time, every field
overridable by an environment variable of the same name). Kept
consistent on purpose: anyone who's already read Project 1's config
style reads this one for free.

Why pydantic-settings and not `os.environ.get(...)` scattered through the
codebase: every setting is declared ONCE, with its type and default, in
one place -- a typo in an env var name becomes a validation error at
startup (fail fast), not a silently-ignored `None` three modules away.

Phase 1 only needs enough settings to start the app and answer `/health`.
Later phases add to this SAME class as they need to (Prometheus scrape
path, OTel exporter endpoint, SQLite path, pricing table path, alert
thresholds, auth) -- flagged inline below so it's clear this file is
expected to grow, not a sign something was forgotten.
"""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Identity -- used as the OTel "service.name" resource attribute from
    # Phase 3 onward, and shown in the dashboard UI so multiple observed
    # services/environments aren't visually confused with each other.
    service_name: str = "obsdash"
    environment: str = "development"

    # HTTP server
    # Loopback by default (Phase 10): reachable only from this machine unless
    # you deliberately change it (Docker sets HOST=0.0.0.0 in Phase 13).
    host: str = "127.0.0.1"
    port: int = 8100  # production-rag defaults to 8000 -- deliberately different so both can run locally at once

    log_level: str = "INFO"

    # Phase 3: empty string -> console exporter (default, zero infra
    # needed). Set to a real OTel Collector's OTLP/HTTP endpoint (e.g.
    # "http://localhost:4318/v1/traces") to export there instead -- see
    # otel_setup.py's docstring for what is and isn't verified about
    # that path in this environment.
    otel_exporter_otlp_endpoint: str = ""

    # Phase 6: bounded SQLite trace store (opened lazily on first use).
    sqlite_path: str = "obsdash.db"
    trace_max_spans: int = 50_000

    # Phase 7: JSON pricing table (see pricing.example.json). Empty ->
    # no prices known -> every model's cost is reported as UNKNOWN.
    pricing_table_path: str = ""

    # Phase 9: alerting. No rules file -> no alerts (nothing invented).
    alert_rules_path: str = ""
    alert_eval_interval_seconds: int = Field(default=30, ge=5, le=3600)
    alert_webhook_url: SecretStr = SecretStr("")  # may embed a secret -> never printed/logged
    alert_max_events: int = 1000
    # Phase 11: OTLP/HTTP receiver limits (an ingestion endpoint must be bounded).
    otlp_max_body_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)
    otlp_max_spans_per_request: int = Field(default=2000, ge=1)
    # Recognise spans from known sibling projects (see obsdash/integrations).
    otlp_enrich_known_services: bool = True

    # Phase 10: access control + retention + resource limits.
    # Empty api_token = open access (fine on loopback). When set, every
    # /api/* route and /metrics needs `Authorization: Bearer <token>`
    # (or `X-API-Key: <token>`). /health stays open for health checks.
    api_token: SecretStr = SecretStr("")
    trace_retention_hours: int = Field(default=168, ge=1, le=8760)
    query_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    @field_validator("api_token")
    @classmethod
    def _token_not_trivial(cls, v: SecretStr) -> SecretStr:
        if v.get_secret_value() and len(v.get_secret_value()) < 16:
            raise ValueError("API_TOKEN must be at least 16 characters (use a long random value)")
        return v

    # -- Added in later phases, not yet used by any code through Phase 3 --
    # prometheus settings (this service's own /metrics)    (Phase 4)


@lru_cache
def get_settings() -> Settings:
    """Cached: `Settings()` re-reads the environment every time it's
    constructed, which is correct for tests (each test can monkeypatch env
    vars and get a fresh read) but wasteful to do on every request inside
    the running app. `get_settings` is what request handlers/dependencies
    use; tests that need a fresh read call `Settings()` directly or clear
    this cache (`get_settings.cache_clear()`)."""
    return Settings()
