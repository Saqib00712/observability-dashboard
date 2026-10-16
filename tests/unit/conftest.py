"""
Autouse fixture: resets the request-id contextvar before AND after every
test. Without this, pytest's synchronous test functions share one
contextvars.Context, so a request_id set in one test (e.g. via the
middleware mid-request) would leak into the next test that never sets
its own -- making test order matter, which it should never do.
"""

import pytest

from obsdash.correlation import reset_request_id


@pytest.fixture(autouse=True)
def _reset_request_id_context():
    reset_request_id()
    yield
    reset_request_id()
