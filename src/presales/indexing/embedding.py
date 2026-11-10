import hashlib
import math
from typing import Protocol

import httpx

from presales.infrastructure.outbound import OutboundAdapter, OutboundService


class EmbeddingFailure(Exception):
    pass


class Embedder(Protocol):
    model_identity: str
    dimensions: int
    mode: str

    def authorize(self, declaration: str) -> None: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedding:
    model_identity = "deterministic-sha256-engineering-v1"
    mode = "fake"

    def __init__(self, outbound: OutboundAdapter, dimensions: int = 8) -> None:
        if not 1 <= dimensions <= 4096:
            raise ValueError("Invalid dimensions")
        self.outbound = outbound
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        values = []
        for text in texts:
            raw = hashlib.shake_256(text.encode()).digest(self.dimensions * 2)
            vector = [
                int.from_bytes(raw[i : i + 2], "big") / 65535 - 0.5 for i in range(0, len(raw), 2)
            ]
            norm = math.sqrt(sum(v * v for v in vector)) or 1
            values.append([v / norm for v in vector])
        return values

    def authorize(self, declaration: str) -> None:
        target = self.outbound.resolve(OutboundService.EMBEDDING, declaration)
        if target.mode != self.mode:
            raise EmbeddingFailure("explicit_fake_configuration_required")


class ConfiguredEmbedding:
    mode = "real"

    def __init__(
        self, outbound: OutboundAdapter, dimensions: int, api_key: str | None = None
    ) -> None:
        target = outbound.describe(OutboundService.EMBEDDING)
        if (
            target.mode != "real"
            or not target.endpoint
            or not target.service_identity
            or not 1 <= dimensions <= 4096
        ):
            raise EmbeddingFailure("explicit_real_configuration_required")
        self.outbound = outbound
        self.model_identity = target.service_identity
        self.dimensions = dimensions
        self._endpoint = target.endpoint
        self._api_key = api_key

    def authorize(self, declaration: str) -> None:
        target = self.outbound.resolve(OutboundService.EMBEDDING, declaration)
        if (
            target.mode != self.mode
            or target.endpoint != self._endpoint
            or target.service_identity != self.model_identity
        ):
            raise EmbeddingFailure("embedding_configuration_changed")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            async with httpx.AsyncClient(
                timeout=60, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.post(
                    self._endpoint,
                    headers=headers,
                    json={
                        "model": self.model_identity,
                        "dimensions": self.dimensions,
                        "input": texts,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            if payload.get("model") != self.model_identity:
                raise ValueError
            values = sorted(payload["data"], key=lambda item: item["index"])
            if [v["index"] for v in values] != list(range(len(texts))):
                raise ValueError
            return [v["embedding"] for v in values]
        except Exception:
            raise EmbeddingFailure("embedding_provider_failed") from None
