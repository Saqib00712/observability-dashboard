"""Fake AI components so telemetry is tested with zero paid API calls."""

from obsdash.ai_telemetry import AITelemetry, OperationKind


class FakeLLM:
    def __init__(self, ai: AITelemetry, usage=(12, 7), fail: Exception | None = None):
        self.ai, self.usage, self.fail = ai, usage, fail

    def chat(self, prompt: str) -> str:
        with self.ai.operation(OperationKind.MODEL_CALL, provider="fake", target="fake-llm") as op:
            if self.fail:
                raise self.fail
            if self.usage is not None:
                op.set_usage(input_tokens=self.usage[0], output_tokens=self.usage[1])
            return "fake reply"


class FakeEmbedder:
    def __init__(self, ai: AITelemetry):
        self.ai = ai

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self.ai.operation(OperationKind.EMBEDDING, provider="fake", target="fake-embed") as op:
            op.set_usage(input_tokens=sum(len(t.split()) for t in texts))
            return [[0.0, 1.0] for _ in texts]


class FakeRetriever:
    def __init__(self, ai: AITelemetry):
        self.ai = ai

    def search(self, query: str) -> list[str]:
        with self.ai.operation(OperationKind.RETRIEVAL, provider="fake", target="fake-index") as op:
            docs = ["doc-a", "doc-b", "doc-c"]
            op.set_result_count(len(docs))
            return docs


class FakeReranker:
    def __init__(self, ai: AITelemetry):
        self.ai = ai

    def rerank(self, docs: list[str]) -> list[str]:
        with self.ai.operation(OperationKind.RERANK, provider="fake", target="fake-rerank") as op:
            op.set_result_count(2)
            return docs[:2]


def fake_tool(ai: AITelemetry, name: str = "calculator"):
    with ai.operation(OperationKind.TOOL_CALL, provider="local", target=name):
        return 42
