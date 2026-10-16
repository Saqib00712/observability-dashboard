# obsdash integration contract (for any system, including Project 3)

Project 3 was not built, so **no Project 3 adapter exists and none is claimed.**
This page is the contract such a system (or any other) can follow. It uses only
interfaces that exist and are tested in this repo.

## Option A — send OpenTelemetry spans (no obsdash code in your app)
`POST {obsdash}/v1/traces`, `Content-Type: application/x-protobuf` (gzip optional).
Standard OTLP exporters do this when given the endpoint. If obsdash has `API_TOKEN`
set, add the header `Authorization: Bearer <token>` (for stock exporters:
`OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <token>"`).
Not supported: OTLP/JSON, OTLP/gRPC, logs, metrics.

To appear in the **AI & cost** views a span must be `CLIENT` kind with
`gen_ai.operation.name` set to one of: `model_call`, `embedding`, `retrieval`,
`rerank`, `tool_call`, `agent_step`. Optional attributes:

| Attribute | Meaning |
|---|---|
| `gen_ai.system` | provider name (low cardinality) |
| `obsdash.ai.target` | model or component name (low cardinality; capped at 100 distinct) |
| `gen_ai.usage.input_tokens` / `output_tokens` | token counts (omit if unknown) |
| `obsdash.ai.cost_amount` + `obsdash.ai.cost_currency` + `obsdash.ai.cost_status=known` | only if YOU computed a real cost |
| `obsdash.ai.cost_status=no_usage` / `unknown_model` | cost unknown; counted as "unpriced", never as 0 |

Set span status ERROR for failures. Never put prompts, documents or credentials in
attributes (obsdash redacts patterns, but redaction is best effort).

## Option B — in-process (Python)
`obsdash.ai_telemetry.AITelemetry(...).operation(kind, provider=, target=)` — see Phase 5.

## What a Project 3 adapter would need
Its real source/interfaces first. Then either Option A with its own exporter config, or a
small observer/wrapper like `obsdash/integrations/eval_harness_observer.py` written against
its actual hooks. Do not guess hook names.
