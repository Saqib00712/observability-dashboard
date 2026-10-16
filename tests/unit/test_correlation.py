"""
Phase 2: the contextvar-based request-id primitive, independent of any
HTTP layer (the middleware tests cover the HTTP-facing behavior).
"""

from obsdash.correlation import get_request_id, new_request_id, set_request_id


def test_new_request_id_is_unique_each_call():
    a = new_request_id()
    b = new_request_id()
    assert a != b


def test_get_request_id_is_none_before_any_is_set():
    # No set_request_id() call has happened in this test's own context yet.
    assert get_request_id() is None


def test_set_then_get_round_trips():
    set_request_id("abc-123")
    assert get_request_id() == "abc-123"
