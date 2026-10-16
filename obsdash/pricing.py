"""
Phase 7: pricing table + cost computation.

Honesty rules (the whole point of this module):
1. NO built-in prices. Provider prices change and I cannot verify today's
   numbers, so shipping "real-looking" ones would be inventing data. You
   supply a JSON file (`PRICING_TABLE_PATH`); see `pricing.example.json`
   (fake provider, example numbers only).
2. Unknown model -> cost is UNKNOWN, never 0. A zero would silently
   under-report spend.
3. Missing token counts -> cost is UNKNOWN, never estimated.
4. Money uses `Decimal`, not float, so 0.1 + 0.2 style drift can't creep
   into a bill. (Prometheus/OTel attributes need floats; the conversion
   happens only at the edge, rounded.)

Prices are "per million tokens", the unit providers publish. A model may
leave `output_per_million` null (e.g. embeddings): then only input tokens
are required to compute its cost.
"""

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

_MILLION = Decimal(1_000_000)


class CostStatus(str, Enum):
    KNOWN = "known"
    UNKNOWN_MODEL = "unknown_model"  # no price entry for (provider, model)
    NO_USAGE = "no_usage"  # provider didn't report the tokens needed


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: Decimal | None
    output_per_million: Decimal | None
    currency: str = "USD"
    effective_date: str | None = None


@dataclass(frozen=True)
class CostResult:
    status: CostStatus
    amount: Decimal | None = None
    currency: str | None = None


def _to_price(value, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"pricing: {field} is not a number: {value!r}") from None
    if not d.is_finite() or d < 0:
        raise ValueError(f"pricing: {field} must be a finite number >= 0, got {value!r}")
    return d


class PricingTable:
    def __init__(self, prices: dict[tuple[str, str], ModelPrice] | None = None) -> None:
        self._prices = dict(prices or {})

    def __len__(self) -> int:
        return len(self._prices)

    @classmethod
    def from_dict(cls, data: dict) -> "PricingTable":
        entries = data.get("models") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ValueError('pricing: top level must be {"models": [...]}')
        prices: dict[tuple[str, str], ModelPrice] = {}
        for i, e in enumerate(entries):
            if not isinstance(e, dict) or not e.get("provider") or not e.get("model"):
                raise ValueError(f"pricing: entry {i} needs 'provider' and 'model'")
            key = (str(e["provider"]), str(e["model"]))
            if key in prices:
                raise ValueError(f"pricing: duplicate entry for {key[0]}/{key[1]}")
            pin = _to_price(e.get("input_per_million"), f"{key[1]}.input_per_million")
            pout = _to_price(e.get("output_per_million"), f"{key[1]}.output_per_million")
            if pin is None and pout is None:
                raise ValueError(f"pricing: {key[0]}/{key[1]} has no price at all")
            prices[key] = ModelPrice(pin, pout, str(e.get("currency", "USD")), e.get("effective_date"))
        return cls(prices)

    @classmethod
    def from_file(cls, path: str) -> "PricingTable":
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"pricing: cannot read {path}: {exc}") from None
        try:
            return cls.from_dict(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError(f"pricing: {path} is not valid JSON: {exc}") from None

    def compute(
        self, provider: str, model: str, input_tokens: int | None, output_tokens: int | None
    ) -> CostResult:
        price = self._prices.get((provider, model))
        if price is None:
            return CostResult(CostStatus.UNKNOWN_MODEL)
        total = Decimal(0)
        for tokens, per_million in (
            (input_tokens, price.input_per_million),
            (output_tokens, price.output_per_million),
        ):
            if per_million is None:
                continue  # this direction isn't priced -> not needed
            if tokens is None:
                return CostResult(CostStatus.NO_USAGE)
            total += Decimal(tokens) * per_million / _MILLION
        return CostResult(CostStatus.KNOWN, total, price.currency)
