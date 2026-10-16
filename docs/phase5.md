# Phase 5 — AI-specific telemetry

## What was built
- `obsdash/ai_telemetry.py` — `AITelemetry.operation(kind, provider=, target=)` context manager for MODEL_CALL, EMBEDDING, RETRIEVAL, RERANK, TOOL_CALL, AGENT_STEP. Opens a child span, times it, records ok/error, counts tokens only when supplied.
- `obsdash/metrics.py` — new: `obsdash_ai_operations_total{operation,provider,target,status}`, `obsdash_ai_operation_duration_seconds{operation,provider,target}`, `obsdash_ai_tokens_total{provider,target,direction}`.
- `app.state.ai` — the handle Phase 11 integrations will use.
- `tests/fakes/ai_fakes.py` — FakeLLM/Embedder/Retriever/Reranker/tool. No paid API calls.
- `pytest.ini` — `pythonpath = .` so tests run from any directory.

## Concepts
- **GenAI span attributes**: `gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens/output_tokens` (OpenTelemetry naming).
- **Nesting**: an agent step span contains model-call and tool spans, which sit under the request span — one trace shows the whole chain.
- **Token honesty**: if the provider reports no usage, nothing is recorded. A fake `0` would read as "free"; an estimate would read as fact.
- **Privacy**: the recorder has no way to attach prompt/response text or tool arguments.
- **Cardinality**: `target` must come from a small known set (model/tool/index/agent names), never user input.

## Commands
```
python -m pytest -q
```

## Results
43 passed (34 + 9 new). Also exercised through the real `/metrics` endpoint: one fake model call produced operations_total=1, duration histogram, tokens input=12 / output=7.

## Honest limits
- Only fakes tested; no real provider SDK is wrapped yet (Phase 11).
- `target` cardinality is a caller contract, not enforced in code.
- Cost is not computed here (Phase 7).
- Streaming calls (tokens arriving at the end) work with set_usage once the stream finishes; time-to-first-token is not measured.
