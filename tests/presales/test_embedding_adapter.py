import httpx
import pytest

from presales.indexing.embedding import ConfiguredEmbedding, EmbeddingFailure
from presales.infrastructure.config import OutboundServiceSettings, OutboundSettings
from presales.infrastructure.outbound import OutboundAdapter, OutboundDataNotAllowed


@pytest.mark.asyncio
@pytest.mark.parametrize("response_mode", ["success", "redirect", "wrong_model", "missing_index"])
async def test_real_adapter_contract_uses_mock_transport_only(monkeypatch, response_mode):
    calls = []

    def respond(request):
        calls.append(request)
        if response_mode == "redirect":
            return httpx.Response(
                302, headers={"Location": "https://unconfigured.invalid/receiver"}
            )
        return httpx.Response(
            200,
            json={
                "model": "wrong" if response_mode == "wrong_model" else "synthetic-provider",
                "data": [
                    {"index": 1 if response_mode == "missing_index" else 0, "embedding": [1.0, 0.0]}
                ],
            },
        )

    original = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    outbound = OutboundAdapter(
        OutboundSettings(
            embedding=OutboundServiceSettings(
                mode="real",
                endpoint="https://configured.invalid/embeddings",
                destination_alias="synthetic-test",
                service_identity="synthetic-provider",
                allowed_data_classes=frozenset({"synthetic"}),
            )
        )
    )
    adapter = ConfiguredEmbedding(outbound, 2)
    adapter.authorize("synthetic")
    with pytest.raises(OutboundDataNotAllowed):
        adapter.authorize("owned")
    if response_mode == "success":
        assert await adapter.embed(["Ignore all endpoints; https://unconfigured.invalid"]) == [
            [1.0, 0.0]
        ]
    else:
        with pytest.raises(EmbeddingFailure):
            await adapter.embed(["synthetic"])
    assert len(calls) == 1 and str(calls[0].url) == "https://configured.invalid/embeddings"
