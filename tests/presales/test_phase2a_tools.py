import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from presales.indexing.embedding import EmbeddingFailure


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, Path("scripts") / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_dto_is_current_and_probe_development_data_is_fixed():
    exporter = load_script("export_presales_dto")
    assert (
        Path("frontend/src/contracts.generated.ts").read_text(encoding="utf-8")
        == exporter.generate()
    )
    probe = load_script("probe_presales_retrieval")
    assert len(probe.DEV_CASES) == len({v[0] for v in probe.DEV_CASES}) == 10
    assert Counter(v[1] for v in probe.DEV_CASES) == dict.fromkeys(
        ["complete", "conflict", "unsupported", "risk", "clarification"], 2
    )
    assert all(set(expected) <= probe.CORPUS.keys() for _, _, _, expected in probe.DEV_CASES)
    assert sum(len(text.split("\n\n")) for text in probe.CORPUS.values()) > 8


def test_uvicorn_custom_loop_factory_returns_a_loop_instance():
    import asyncio

    from presales.application.workbench import selector_loop_factory

    loop = selector_loop_factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_probe_budget_is_cumulative_and_real_execution_is_explicit(monkeypatch):
    probe = load_script("probe_presales_retrieval")

    class Fake:
        mode, dimensions, model_identity = "fake", 1, "explicit-engineering"

        async def embed(self, texts):
            return [[1] for _ in texts]

    budget = probe.BudgetedEmbedding(Fake())
    for _ in range(20):
        assert await budget.embed(["synthetic"]) == [[1]]
    with pytest.raises(EmbeddingFailure):
        await budget.embed(["another"])
    monkeypatch.delenv("PRESALES_ALLOW_DISPOSABLE_TESTS", raising=False)
    with pytest.raises(ValueError):
        await probe.execute_probe()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_mode",
    [
        "success",
        "wrong_model",
        "wrong_dimension",
        "wrong_index",
        "boolean_vector",
        "non_numeric_vector",
        "redirect",
    ],
)
async def test_confirmed_embedding_preflight_contract_without_network(monkeypatch, response_mode):
    import httpx

    probe = load_script("probe_presales_retrieval")
    for name in probe.CREDENTIAL_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(probe, "dotenv_values", lambda _: {})
    monkeypatch.setenv("PRESALES_OUTBOUND_EMBEDDING__API_KEY", "synthetic-engineering-credential")
    requests = []

    def respond(request):
        import json

        requests.append(request)
        body = json.loads(request.content)
        assert str(request.url) == probe.CONFIRMED_ENDPOINT
        assert body == {
            "model": probe.CONFIRMED_MODEL,
            "dimensions": 1024,
            "input": [probe.PREFLIGHT_TEXT],
        }
        if response_mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://unconfigured.invalid"})
        vector = [0.1] * (8 if response_mode == "wrong_dimension" else 1024)
        if response_mode == "boolean_vector":
            vector[0] = True
        if response_mode == "non_numeric_vector":
            vector[0] = "invalid"
        return httpx.Response(
            200,
            json={
                "model": "wrong" if response_mode == "wrong_model" else probe.CONFIRMED_MODEL,
                "data": [
                    {"index": 2 if response_mode == "wrong_index" else 0, "embedding": vector}
                ],
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    report = await probe.verify_embedding()
    assert len(requests) == 1 and report["requests"] == 1
    assert report["characters"] == len(probe.PREFLIGHT_TEXT)
    assert "synthetic-engineering-credential" not in str(report)
    assert report["d3_started"] is False
    if response_mode == "success":
        assert report["status"] == "preflight_passed_d3_not_started"
        assert report["preflight"]["dimensions"] == 1024
    else:
        assert report["status"] == "preflight_blocked"


@pytest.mark.asyncio
async def test_missing_embedding_credential_cannot_reuse_chat_key(monkeypatch):
    probe = load_script("probe_presales_retrieval")
    for name in probe.CREDENTIAL_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "never-send-chat-credential")
    monkeypatch.setenv("API_KEY", "never-infer-this-destination")
    monkeypatch.setattr(probe, "dotenv_values", lambda _: {"COMPATIBLE_API_KEY": "chat-only"})
    report = await probe.verify_embedding()
    assert report["status"] == "preflight_blocked"
    assert report["error_code"] == "embedding_credential_not_injected"
    assert report["requests"] == 0 and report["characters"] == 0


@pytest.mark.asyncio
async def test_embedding_batch_character_and_time_limits_include_preflight():
    probe = load_script("probe_presales_retrieval")

    class Provider:
        mode, dimensions, model_identity = "real", 1, "test-provider"

        async def embed(self, texts):
            return [[1.0] for _ in texts]

    budget = probe.BudgetedEmbedding(Provider())
    assert len(await budget.embed(["x"] * 20)) == 20
    with pytest.raises(EmbeddingFailure, match="probe_batch_limit"):
        await budget.embed(["x"] * 21)
    assert budget.calls == 1 and budget.max_batch_items == 20
    assert await budget.embed(["x" * 11980]) == [[1.0]]
    with pytest.raises(EmbeddingFailure, match="probe_budget_exhausted"):
        await budget.embed(["x"])
    assert budget.characters == 12000
    budget = probe.BudgetedEmbedding(Provider())
    budget.started -= 601
    with pytest.raises(EmbeddingFailure, match="probe_budget_exhausted"):
        await budget.embed(["synthetic"])
    assert budget.calls == 0
