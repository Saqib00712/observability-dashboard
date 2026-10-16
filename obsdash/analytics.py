"""
Phase 8: turns stored spans into the numbers the dashboard shows.

Pure functions over rows (no I/O), so every number is unit-testable with
hand-built rows. Everything is computed from REAL collected spans in the
trace store -- there are no hardcoded or demo values anywhere.

Definitions (stated so the UI can't mislead):
- "request" = a SERVER span (one per HTTP request handled).
- "error"   = a span with status ERROR. For requests that means an
  unhandled exception or a 5xx response; a 404 or 422 is the CLIENT's
  mistake and is NOT counted as a service error.
- latency percentiles use nearest-rank on the durations in the window.
- AI operations = CLIENT spans carrying `gen_ai.operation.name`. The
  headline AI totals count only model calls + embeddings (the token-billed
  kinds); every kind appears in the per-operation table.
- Errors list shows error ORIGINS: one failure marks the whole chain of
  parent spans ERROR (model call -> agent step -> request), so only the
  deepest errored span of each chain is listed, not all three copies.
- "unpriced" = model/embedding calls whose cost status is unknown_model
  or no_usage (Phase 7): reported, never silently priced at 0.
"""

import json
import math

_NS_PER_S = 1_000_000_000


def percentile(sorted_values: list[float], p: float) -> float | None:
    """Nearest-rank percentile of an ascending list; None when empty."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _ms(row) -> float:
    return (row["end_ns"] - row["start_ns"]) / 1e6


def _round(v: float | None, n: int = 2) -> float | None:
    return None if v is None else round(v, n)


def bucket_seconds_for(window_minutes: int) -> int:
    """About 60 buckets per window, never finer than one minute."""
    return max(60, (window_minutes * 60 // 60) // 60 * 60)


def build_overview(rows, window_minutes: int, now_ns: int, bounds: dict) -> dict:
    since_ns = now_ns - window_minutes * 60 * _NS_PER_S
    bucket_s = bucket_seconds_for(window_minutes)
    bucket_ns = bucket_s * _NS_PER_S
    first_bucket = (since_ns // bucket_ns) * bucket_ns
    n_buckets = (now_ns - first_bucket) // bucket_ns + 1

    buckets = [{"requests": 0, "errors": 0, "durs": []} for _ in range(n_buckets)]
    req_durs: list[float] = []
    req_errors = 0
    routes: dict[str, dict] = {}
    ai_ops: dict[tuple, dict] = {}
    ai = {"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "unpriced_calls": 0, "cost": {}}

    for row in rows:
        if row["end_ns"] < since_ns:
            continue
        is_err = row["status"] == "ERROR"
        if row["kind"] == "SERVER":
            dur = _ms(row)
            req_durs.append(dur)
            req_errors += is_err
            idx = min(max((row["end_ns"] - first_bucket) // bucket_ns, 0), n_buckets - 1)
            b = buckets[idx]
            b["requests"] += 1
            b["errors"] += is_err
            b["durs"].append(dur)
            r = routes.setdefault(row["name"], {"requests": 0, "errors": 0, "durs": []})
            r["requests"] += 1
            r["errors"] += is_err
            r["durs"].append(dur)
        elif row["kind"] == "CLIENT":
            attrs = json.loads(row["attributes"])
            op = attrs.get("gen_ai.operation.name")
            if op is None:
                continue
            key = (op, attrs.get("gen_ai.system", "unknown"), attrs.get("obsdash.ai.target", "unknown"))
            o = ai_ops.setdefault(key, {"calls": 0, "errors": 0, "durs": [], "input_tokens": 0,
                                        "output_tokens": 0, "unpriced_calls": 0, "cost": {}})
            dur = _ms(row)
            in_t = int(attrs.get("gen_ai.usage.input_tokens") or 0)
            out_t = int(attrs.get("gen_ai.usage.output_tokens") or 0)
            unpriced = attrs.get("obsdash.ai.cost_status") in ("unknown_model", "no_usage")
            # Top-level AI totals count only token-billed operations (model
            # calls + embeddings); retrieval/rerank/tool/agent steps appear
            # in the per-operation table but are not "model calls".
            aggs = (o, ai) if op in ("model_call", "embedding") else (o,)
            for agg in aggs:
                agg["calls"] += 1
                agg["errors"] += is_err
                agg["input_tokens"] += in_t
                agg["output_tokens"] += out_t
                agg["unpriced_calls"] += unpriced
                if "obsdash.ai.cost_amount" in attrs:
                    cur = attrs.get("obsdash.ai.cost_currency", "?")
                    agg["cost"][cur] = agg["cost"].get(cur, 0.0) + float(attrs["obsdash.ai.cost_amount"])
            o["durs"].append(dur)

    req_durs.sort()
    series = []
    for i, b in enumerate(buckets):
        b["durs"].sort()
        series.append({
            "t_ns": first_bucket + i * bucket_ns,
            "requests": b["requests"],
            "errors": b["errors"],
            "p95_ms": _round(percentile(b["durs"], 95)),
        })

    def cost(d):
        return {c: round(v, 6) for c, v in d.items()}

    total = len(req_durs)
    return {
        "window_minutes": window_minutes,
        "bucket_seconds": bucket_s,
        "requests": {
            "total": total,
            "errors": req_errors,
            "error_rate": _round(req_errors / total, 4) if total else None,
            "latency_ms": {p: _round(percentile(req_durs, v)) for p, v in (("p50", 50), ("p95", 95), ("p99", 99))},
        },
        "ai": {
            "calls": ai["calls"], "errors": ai["errors"],
            "input_tokens": ai["input_tokens"], "output_tokens": ai["output_tokens"],
            "unpriced_calls": ai["unpriced_calls"], "cost_by_currency": cost(ai["cost"]),
        },
        "series": series,
        "routes": sorted(
            ({"route": k, "requests": v["requests"], "errors": v["errors"],
              "p95_ms": _round(percentile(sorted(v["durs"]), 95))} for k, v in routes.items()),
            key=lambda x: -x["requests"])[:50],
        "ai_operations": sorted(
            ({"operation": k[0], "provider": k[1], "target": k[2], "calls": v["calls"], "errors": v["errors"],
              "p50_ms": _round(percentile(sorted(v["durs"]), 50)), "p95_ms": _round(percentile(sorted(v["durs"]), 95)),
              "input_tokens": v["input_tokens"], "output_tokens": v["output_tokens"],
              "unpriced_calls": v["unpriced_calls"], "cost_by_currency": cost(v["cost"])}
             for k, v in ai_ops.items()),
            key=lambda x: -x["calls"])[:50],
        "data_status": {
            **bounds,
            "window_has_data": total > 0 or ai["calls"] > 0,
            "empty": bounds["spans_in_store"] == 0,
        },
    }


def build_errors(rows, window_minutes: int, now_ns: int, limit: int) -> dict:
    since_ns = now_ns - window_minutes * 60 * _NS_PER_S
    recent, groups = [], {}
    err_rows = [r for r in rows if r["status"] == "ERROR" and r["end_ns"] >= since_ns]
    erroring_parents = {(r["trace_id"], r["parent_id"]) for r in err_rows if r["parent_id"]}
    for row in reversed(err_rows):  # rows ascend by start; we want newest first
        if (row["trace_id"], row["span_id"]) in erroring_parents:
            continue  # an errored child exists: this span is only propagating it
        attrs = json.loads(row["attributes"])
        error_type = attrs.get("error.type") or row["status_message"] or "unknown"
        g = groups.setdefault((row["name"], error_type), {"name": row["name"], "error_type": error_type,
                                                          "kind": row["kind"], "count": 0, "last_ns": row["start_ns"]})
        g["count"] += 1
        if len(recent) < limit:
            recent.append({
                "trace_id": row["trace_id"], "span_id": row["span_id"], "name": row["name"],
                "service": row["service"], "kind": row["kind"], "time_ns": row["start_ns"],
                "duration_ms": _round(_ms(row)), "error_type": error_type,
            })
    return {
        "window_minutes": window_minutes,
        "total": sum(g["count"] for g in groups.values()),
        "groups": sorted(groups.values(), key=lambda g: -g["count"])[:50],
        "recent": recent,
    }
