import time
from uuid import uuid4

import jwt
import pytest
from pydantic import SecretStr

from presales.domain.contracts import AccessDenied, Actor, AuthenticationFailed
from presales.infrastructure.product_catalog import (
    CatalogAdapter,
    CatalogClient,
    CatalogCredential,
    create_catalog_server,
)
from presales.knowledge.models import Applicability
from presales.retrieval.models import RetrievalResult
from tests.presales.test_retrieval_units import query

KEY = SecretStr("synthetic-catalog-signing-key-for-engineering-only")


@pytest.mark.asyncio
async def test_catalog_current_gate_empty_and_failure_are_distinct():
    class Retrieval:
        allowed = True

        async def retrieve_evidence(self, actor, request):
            if not self.allowed:
                raise AccessDenied()
            return RetrievalResult(
                run_id=uuid4(),
                mode="fake",
                model_identity="explicit-engineering",
                authorization_revision=1,
                catalog_revision=1,
                cache_hit=False,
                degradation=[],
                hits=[],
            )

    retrieval = Retrieval()
    credential = CatalogCredential(KEY)
    actor = Actor(subject=uuid4())
    request = query()
    token = credential.issue(actor, request.scope)
    adapter = CatalogAdapter(retrieval, credential)
    assert (await adapter.lookup(request, token)).status == "empty"
    retrieval.allowed = False
    assert (await adapter.lookup(request, token)).status == "not_found_or_not_allowed"
    assert (await adapter.lookup(request, "forged")).status == "not_found_or_not_allowed"
    assert credential.verify(token, request) == actor
    with pytest.raises(AuthenticationFailed):
        credential.verify(token, query(scope=Applicability(products=("*",), deployments=("*",))))


@pytest.mark.parametrize(
    "override",
    [{"aud": "presales-browser"}, {"exp": 1}, {"grant": "all"}, {"exp": int(time.time()) + 3600}],
)
def test_catalog_credentials_reject_wrong_audience_expiry_and_extra_claims(override):
    now = int(time.time())
    claims = {
        "sub": str(uuid4()),
        "iss": "presales-backend",
        "aud": "presales-product-catalog",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "scope": query().scope.model_dump(mode="json"),
        **override,
    }
    token = jwt.encode(claims, KEY.get_secret_value(), algorithm="HS256")
    with pytest.raises(AuthenticationFailed):
        CatalogCredential(KEY).verify(token, query())


@pytest.mark.asyncio
async def test_only_read_tool_registered_and_environment_is_minimal():
    server = create_catalog_server(CatalogAdapter(None, CatalogCredential(KEY)))
    tools = await server.list_tools()
    assert [tool.name for tool in tools] == ["lookup_capabilities"]
    schema = tools[0].inputSchema
    assert set(schema["properties"]) == {"request"}
    assert "presales_context" not in str(schema)
    client = CatalogClient(
        CatalogCredential(KEY),
        {
            "PRESALES_DATABASE_URL": "synthetic",
            "OPENAI_API_KEY": "never inherit",
            "AUTH_SECRET": "never inherit",
        },
    )
    assert "OPENAI_API_KEY" not in client.environment and "AUTH_SECRET" not in client.environment
    assert client.environment["PRESALES_DATABASE_URL"] == "synthetic"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_product_catalog_stdio_real_roundtrip_and_revoked_replay(services):
    import os

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from tests.presales.phase2a_helpers import SALES, publish
    from tests.presales.test_retrieval_postgres import revoke

    registry, _, index, _, document, _, _ = await publish(services)
    credential = CatalogCredential(KEY)
    environment = {
        "PRESALES_DATABASE_URL": os.environ["PRESALES_TEST_RUNTIME_URL"],
        "PRESALES_FILE_ROOT": str(services[0].files.root),
        "PRESALES_CATALOG_JWT_SECRET": KEY.get_secret_value(),
        "PRESALES_OUTBOUND_EMBEDDING__MODE": "fake",
        "PRESALES_OUTBOUND_EMBEDDING__ALLOWED_DATA_CLASSES": '["synthetic"]',
        "PRESALES_RETRIEVAL_DIMENSIONS": "8",
    }
    client = CatalogClient(credential, environment)
    request = query(query="capability")
    from presales.retrieval.service import RetrievalService

    direct = await RetrievalService(registry, index.embedding).retrieve_evidence(SALES, request)
    remote = await client.lookup(SALES, request)
    assert remote.status == "ok" and remote.result is not None, remote.model_dump()
    assert [v.evidence_id for v in remote.result.hits] == [v.evidence_id for v in direct.hits]
    import sys

    async with stdio_client(
        StdioServerParameters(
            command=sys.executable,
            args=["-m", "presales.infrastructure.product_catalog"],
            env=client.environment,
        )
    ) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            args = {"request": request.model_dump(mode="json")}
            missing = await session.call_tool("lookup_capabilities", args)
            assert missing.structuredContent["status"] == "not_found_or_not_allowed"
            token = credential.issue(SALES, request.scope)
            meta = {"presales_context": token}
            forged = await session.call_tool(
                "lookup_capabilities",
                {"request": {**args["request"], "actor": "forged"}},
                meta=meta,
            )
            assert forged.isError
            write = await session.call_tool("publish_knowledge", {}, meta=meta)
            assert write.isError
            await revoke(services, document)
            replay = await session.call_tool("lookup_capabilities", args, meta=meta)
            assert replay.structuredContent["status"] == "empty"
