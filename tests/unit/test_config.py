"""
Phase 1: config defaults are sane, and environment variables actually
override them -- the entire point of using pydantic-settings instead of
hardcoding values. If this test ever fails, every later phase's "just
set FOO_BAR in your .env" instructions would be lying.
"""

import os

from obsdash.config import Settings, get_settings


def test_defaults_are_set_without_any_env_vars(monkeypatch):
    for key in ("SERVICE_NAME", "ENVIRONMENT", "HOST", "PORT", "LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)  # ignore any real .env on disk for this test
    assert s.service_name == "obsdash"
    assert s.environment == "development"
    assert s.port == 8100
    assert s.log_level == "INFO"


def test_environment_variable_overrides_default(monkeypatch):
    monkeypatch.setenv("SERVICE_NAME", "obsdash-test-instance")
    monkeypatch.setenv("PORT", "9999")
    s = Settings(_env_file=None)
    assert s.service_name == "obsdash-test-instance"
    assert s.port == 9999


def test_get_settings_is_cached():
    get_settings.cache_clear()
    a = get_settings()
    b = get_settings()
    assert a is b  # same object -- proves the lru_cache is actually caching, not just "equal values"
