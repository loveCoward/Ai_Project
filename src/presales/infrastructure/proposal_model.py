"""Explicit OpenAI-compatible/local model transport; no environment credentials or fallback."""

import asyncio
from collections.abc import Callable

import httpx

from presales.application.proposal_attempt_context import ProposalAttemptContext
from presales.domain.contracts import DomainError, digest
from presales.infrastructure.config import OutboundServiceSettings, OutboundSettings
from presales.infrastructure.database import rows
from presales.infrastructure.outbound import (
    OutboundAdapter,
    OutboundConfigurationError,
    OutboundService,
)


class ProposalModelFailure(DomainError):
    code = "proposal_model_failed"


class ProposalModel:
    def __init__(
        self,
        settings: OutboundServiceSettings,
        config_identity: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        fake: Callable[[str], str] | None = None,
    ) -> None:
        self.settings, self.config_identity = settings, config_identity
        self.transport, self.fake = transport, fake
        self.outbound = OutboundAdapter(OutboundSettings(language_model=settings))

    def preflight(self, attempt: dict, declarations: set[str]) -> None:
        if not declarations or attempt["config_identity"] != self.config_identity:
            raise OutboundConfigurationError("Model configuration mismatch")
        for declaration in declarations:
            target = self.outbound.resolve(OutboundService.LANGUAGE_MODEL, declaration)
            if target.mode != attempt["model_mode"] or (
                target.mode == "fake" and self.fake is None
            ):
                raise OutboundConfigurationError("Explicit model mode required")
        if self.settings.mode == "real":
            url = httpx.URL(str(self.settings.endpoint))
            if url.username or url.password or url.query or url.fragment:
                raise OutboundConfigurationError("Endpoint must not contain credentials or query")
            if url.scheme != "https" and url.host not in {"127.0.0.1", "localhost", "::1"}:
                raise OutboundConfigurationError("Remote endpoint requires HTTPS")
            if url.host not in {"127.0.0.1", "localhost", "::1"} and not self.settings.api_key:
                raise OutboundConfigurationError("Remote model requires explicit credentials")

    async def generate(
        self, context: ProposalAttemptContext, active, node: str, sources, prompt: str
    ) -> str:
        await context.check(sources)
        async with context.boundary(sources) as (conn, attempt, _):
            declarations = set()
            for source in sources:
                declarations.update(
                    v["declaration"]
                    for v in await rows(
                        conn,
                        """WITH RECURSIVE deps(id) AS (SELECT CAST(:id AS uuid) UNION
                    SELECT d.upstream_id FROM presales.direct_dependencies d JOIN deps ON d.downstream_id=deps.id)
                    SELECT declaration FROM presales.resource_versions WHERE id IN (SELECT id FROM deps) AND source_kind='original'""",
                        id=source,
                    )
                )
            self.preflight(attempt, declarations)
        call = await context.register_call(
            active, node, "model", sources, digest([prompt, sources, self.config_identity])
        )
        timeout = call.body["deadline"] - context.clock().timestamp()
        try:
            async with asyncio.timeout(timeout):
                if self.settings.mode == "fake":
                    result = self.fake(prompt) if self.fake else ""
                else:
                    headers = (
                        {"Authorization": f"Bearer {self.settings.api_key.get_secret_value()}"}
                        if self.settings.api_key
                        else {}
                    )
                    async with httpx.AsyncClient(
                        transport=self.transport,
                        timeout=timeout,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client:
                        response = await client.post(
                            str(self.settings.endpoint),
                            headers=headers,
                            json={
                                "model": self.settings.service_identity,
                                "messages": [{"role": "user", "content": prompt}],
                                "stream": False,
                            },
                        )
                        response.raise_for_status()
                        payload = response.json()
                        if payload["model"] != self.settings.service_identity:
                            raise ValueError("Model identity mismatch")
                        result = payload["choices"][0]["message"]["content"]
                if not isinstance(result, str) or not result.strip():
                    raise ValueError("Empty model response")
        except asyncio.CancelledError:
            await context.finish_call(call, status="unknown")
            raise
        except (httpx.TransportError, TimeoutError):
            await context.finish_call(call, status="unknown")
            raise ProposalModelFailure() from None
        except Exception as error:
            retryable = isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {
                429,
                502,
                503,
                504,
            }
            await context.finish_call(call, status="failed", retryable=retryable)
            raise ProposalModelFailure() from None
        await context.finish_call(call)
        return result
