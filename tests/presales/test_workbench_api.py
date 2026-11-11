from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from presales.access.jwt import JwtIssuer, JwtVerifier
from presales.api.auth import create_auth_app
from presales.application.auth import AuthenticationService
from presales.application.workbench import Workbench
from presales.infrastructure.config import JwtSettings
from presales.infrastructure.principals import DatabasePrincipalStore
from presales.retrieval.service import RetrievalService
from tests.presales.phase2a_helpers import ARCH, REVIEWER, SALES, publish
from tests.presales.test_retrieval_postgres import revoke
from tests.presales.test_retrieval_units import query


def app_for(services, registry, index):
    settings = JwtSettings(
        secret=SecretStr("synthetic-workbench-api-signing-key"),
        issuer="test",
        audience="test-browser",
    )
    auth = AuthenticationService(DatabasePrincipalStore(services[0].database), settings, ())
    app = create_auth_app(
        auth,
        JwtVerifier(settings),
        business_service=services[0],
        workbench=Workbench(services[0], RetrievalService(registry, index.embedding)),
    )
    return app, JwtIssuer(settings)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_workbench_api_uses_real_permissions_and_rejects_spoofing(services):
    registry, _, index, _, document, version, _ = await publish(services)
    app, issuer = app_for(services, registry, index)
    headers = {"Authorization": f"Bearer {issuer.issue(SALES)}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        overview = await client.get("/api/workbench", headers=headers)
        assert overview.status_code == 200 and overview.json()["retrieval_mode"] == "fake"
        assert overview.headers["cache-control"] == "no-store"
        result = await client.post(
            "/api/evidence/retrieve",
            headers=headers,
            json=query(query="capability").model_dump(mode="json"),
        )
        assert result.status_code == 200 and result.json()["hits"]
        evidence_id = result.json()["hits"][0]["evidence_id"]
        assert (
            await client.get(f"/api/evidence/{evidence_id}", headers=headers)
        ).status_code == 200
        for field in ("actor", "endpoint", "analysis_attempt_id", "grant"):
            body = {**query().model_dump(mode="json"), field: "forged"}
            assert (
                await client.post("/api/evidence/retrieve", json=body, headers=headers)
            ).status_code == 422
        assert (await client.get("/api/workbench?actor=forged", headers=headers)).status_code == 422
        assert (
            await client.post(
                f"/api/sources/{version}/parse", headers=headers, json={"actor": "forged"}
            )
        ).status_code == 422
        assert (
            await client.post(f"/api/sources/{version}/parse", headers=headers)
        ).status_code == 404
        assert (
            await client.post(f"/api/knowledge/{version}/index", headers=headers, json={})
        ).status_code == 404
        assert (
            await client.post(
                "/api/catalog/lookup", headers=headers, json=query().model_dump(mode="json")
            )
        ).status_code == 503
        await revoke(services, document)
        assert (
            await client.get(f"/api/evidence/{evidence_id}", headers=headers)
        ).status_code == 404
        assert (await client.get(f"/api/knowledge/{version}", headers=headers)).status_code == 404
        assert not (
            await client.post(
                "/api/evidence/retrieve", headers=headers, json=query().model_dump(mode="json")
            )
        ).json()["hits"]
        assert (await client.get("/api/workbench")).status_code == 401
        assert (
            await client.get("/api/workbench", headers={"Authorization": "Bearer forged"})
        ).status_code == 401


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_workbench_review_cannot_be_forged_or_self_approved(services):
    from tests.presales.phase2a_helpers import candidate

    registry, _, index, _, _, version = await candidate(services)
    app, issuer = app_for(services, registry, index)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"Authorization": f"Bearer {issuer.issue(ARCH)}"}
        build = await client.post(f"/api/knowledge/{version}/index", json={}, headers=headers)
        assert build.status_code == 200 and build.json()["state"] == "ready"
        submitted = await client.post(
            f"/api/knowledge/{version}/submit",
            headers=headers,
            json={
                "build_id": build.json()["build_id"],
                "expected_revision": 1,
                "expected_catalog": 0,
                "request_id": str(uuid4()),
            },
        )
        assert submitted.status_code == 200
        path = f"/api/knowledge-reviews/{submitted.json()['review_id']}/decision"
        body = {"approved": True, "reason": "Synthetic approval", "request_id": str(uuid4())}
        assert (await client.post(path, headers=headers, json=body)).status_code == 404
        reviewer = {"Authorization": f"Bearer {issuer.issue(REVIEWER)}"}
        approved = await client.post(path, headers=reviewer, json=body)
        assert approved.status_code == 200 and approved.json()["state"] == "published"
        assert (await client.post(path, headers=reviewer, json=body)).json() == approved.json()
        detail = await client.get(f"/api/knowledge/{version}", headers=reviewer)
        assert detail.json()["reviews"][0]["approved"] is True


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_workbench_upload_parse_manifest_and_idempotent_receipts(services):
    import base64

    from tests.presales.phase2a_helpers import OPP, SCOPE, candidate

    registry, _, index, _, document, _ = await candidate(services)
    app, issuer = app_for(services, registry, index)
    architect = {"Authorization": f"Bearer {issuer.issue(ARCH)}"}
    sales = {"Authorization": f"Bearer {issuer.issue(SALES)}"}
    task = await services[0].create_task(ARCH, OPP, "Synthetic upload workflow", uuid4())
    task_id = task["resource_id"]
    body = {
        "filename": "new.md",
        "content_base64": base64.b64encode(
            b"# Synthetic new upload\n\nPrivate audit support"
        ).decode(),
        "declaration": "synthetic",
        "request_id": str(uuid4()),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/api/knowledge/documents/{document}/versions"
        knowledge_body = {**body, "scope": SCOPE.model_dump(mode="json")}
        assert (await client.post(path, json=knowledge_body, headers=sales)).status_code == 404
        assert (
            await client.post(
                path, json={**knowledge_body, "content_base64": "???"}, headers=architect
            )
        ).status_code == 422
        created = await client.post(path, json=knowledge_body, headers=architect)
        assert created.status_code == 200
        assert (
            await client.post(path, json=knowledge_body, headers=architect)
        ).json() == created.json()
        assert (
            await client.post(
                path, json={**knowledge_body, "filename": "changed.md"}, headers=architect
            )
        ).status_code == 409
        parsed = await client.post(
            f"/api/sources/{created.json()['version_id']}/parse", headers=architect
        )
        assert parsed.json()["state"] == "succeeded"
        material = await client.post(
            f"/api/materials/{task_id}/versions",
            json={**body, "request_id": str(uuid4()), "expected_task_revision": 1},
            headers=architect,
        )
        assert material.status_code == 200
        version = material.json()["material_version_id"]
        manifest = {
            "expected_revision": 2,
            "items": [{"material_version_id": version, "necessity": "required"}],
        }
        assert (
            await client.post(f"/api/tasks/{task_id}/manifest", json=manifest, headers=sales)
        ).status_code == 404
        assert (
            await client.post(f"/api/tasks/{task_id}/manifest", json=manifest, headers=architect)
        ).status_code == 200
        assert (await client.post(f"/api/sources/{version}/parse", headers=architect)).json()[
            "state"
        ] == "succeeded"
        status = (await client.get(f"/api/tasks/{task_id}/inputs", headers=architect)).json()
        assert status["complete"] is True and status["materials"][0]["state"] == "succeeded"
        assert (
            await client.post(f"/api/tasks/{task_id}/manifest", json=manifest, headers=architect)
        ).status_code == 409
