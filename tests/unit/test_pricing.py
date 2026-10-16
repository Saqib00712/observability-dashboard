"""Phase 7: pricing table + cost computation (pure logic, no AI calls)."""

import json
from decimal import Decimal

import pytest

from obsdash.pricing import CostStatus, PricingTable

TABLE = {"models": [
    {"provider": "p", "model": "chat", "input_per_million": "1.00", "output_per_million": "2.00"},
    {"provider": "p", "model": "embed", "input_per_million": "0.10", "output_per_million": None},
    {"provider": "p", "model": "free", "input_per_million": 0, "output_per_million": 0},
]}


def table():
    return PricingTable.from_dict(TABLE)


def test_known_cost_is_exact_decimal():
    r = table().compute("p", "chat", 1_000_000, 500_000)
    assert r.status is CostStatus.KNOWN and r.amount == Decimal("2.00") and r.currency == "USD"


def test_no_float_drift():
    r = table().compute("p", "chat", 3, 0)  # 3 * 1.00 / 1e6
    assert r.amount == Decimal("0.000003")


def test_unknown_model_is_unknown_not_zero():
    r = table().compute("p", "mystery", 100, 100)
    assert r.status is CostStatus.UNKNOWN_MODEL and r.amount is None


def test_same_model_name_other_provider_is_unknown():
    assert table().compute("other", "chat", 1, 1).status is CostStatus.UNKNOWN_MODEL


def test_missing_tokens_is_no_usage_not_estimated():
    t = table()
    assert t.compute("p", "chat", None, None).status is CostStatus.NO_USAGE
    assert t.compute("p", "chat", 100, None).status is CostStatus.NO_USAGE  # output priced but unreported


def test_unpriced_direction_not_required():
    r = table().compute("p", "embed", 1_000_000, None)
    assert r.status is CostStatus.KNOWN and r.amount == Decimal("0.10")


def test_genuinely_free_model_is_known_zero():
    r = table().compute("p", "free", 10, 10)
    assert r.status is CostStatus.KNOWN and r.amount == 0


def test_empty_table_knows_nothing():
    assert PricingTable().compute("p", "chat", 1, 1).status is CostStatus.UNKNOWN_MODEL


@pytest.mark.parametrize("bad", [
    {"models": "nope"},
    {"models": [{"provider": "p"}]},
    {"models": [{"provider": "p", "model": "m"}]},  # no price at all
    {"models": [{"provider": "p", "model": "m", "input_per_million": "-1"}]},
    {"models": [{"provider": "p", "model": "m", "input_per_million": "abc"}]},
    {"models": [{"provider": "p", "model": "m", "input_per_million": "NaN"}]},
    {"models": [{"provider": "p", "model": "m", "input_per_million": 1},
                {"provider": "p", "model": "m", "input_per_million": 2}]},
])
def test_invalid_tables_fail_fast(bad):
    with pytest.raises(ValueError):
        PricingTable.from_dict(bad)


def test_from_file_errors_are_clear(tmp_path):
    with pytest.raises(ValueError, match="cannot read"):
        PricingTable.from_file(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        PricingTable.from_file(str(bad))
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(TABLE))
    assert len(PricingTable.from_file(str(ok))) == 3


def test_shipped_example_file_loads():
    assert len(PricingTable.from_file("pricing.example.json")) == 2
