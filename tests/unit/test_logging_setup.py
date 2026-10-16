"""
Phase 2: the JSON formatter produces valid, parseable JSON with the
required correlation fields -- and does it correctly whether or not a
request_id is currently set, and whether or not the log call included
exception info.
"""

import json
import logging

from obsdash.correlation import set_request_id
from obsdash.logging_setup import CorrelationFilter, JsonFormatter


def _make_record(msg="something happened", level=logging.INFO, exc_info=None, extra=None):
    record = logging.LogRecord(
        name="obsdash.test", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=exc_info,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


def test_formatted_line_is_valid_json():
    record = _make_record()
    CorrelationFilter().filter(record)
    line = JsonFormatter().format(record)
    parsed = json.loads(line)  # raises if not valid JSON
    assert parsed["message"] == "something happened"


def test_required_baseline_fields_are_present():
    record = _make_record()
    CorrelationFilter().filter(record)
    parsed = json.loads(JsonFormatter().format(record))
    for field in ("timestamp", "severity", "service", "environment", "request_id", "trace_id", "span_id"):
        assert field in parsed


def test_request_id_is_attached_when_set():
    set_request_id("req-abc-123")
    record = _make_record()
    CorrelationFilter().filter(record)
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["request_id"] == "req-abc-123"


def test_request_id_is_null_when_not_set():
    record = _make_record()
    CorrelationFilter().filter(record)
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["request_id"] is None


def test_trace_id_and_span_id_are_null_in_phase_2():
    # Phase 3 populates these -- Phase 2 reserves the fields but has no
    # real tracing yet, so they must be present and None, not missing.
    record = _make_record()
    CorrelationFilter().filter(record)
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["trace_id"] is None
    assert parsed["span_id"] is None


def test_extra_fields_ride_along():
    record = _make_record(extra={"duration_ms": 42, "operation": "GET /health"})
    CorrelationFilter().filter(record)
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["duration_ms"] == 42
    assert parsed["operation"] == "GET /health"


def test_exception_info_becomes_safe_category_and_message_not_a_raw_traceback():
    try:
        raise ValueError("a specific internal detail")
    except ValueError:
        import sys
        record = _make_record(exc_info=sys.exc_info())
    CorrelationFilter().filter(record)
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["error_category"] == "ValueError"
    assert parsed["error_message"] == "a specific internal detail"
    # No raw traceback text/frames leaked into the JSON payload.
    assert "Traceback" not in json.dumps(parsed)
