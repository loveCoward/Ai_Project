import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from presales.access.jwt import JwtIssuer, JwtVerifier
from presales.api.app import _load_compatibility_app, create_app
from presales.api.auth import create_auth_app
from presales.application.auth import AuthenticationService
from presales.domain.contracts import AccessDenied, Actor, PrincipalStatus, Role
from presales.infrastructure.config import (
    JwtSettings,
    OutboundServiceSettings,
    OutboundSettings,
    PresalesApiSettings,
)
from presales.infrastructure.outbound import (
    OutboundAdapter,
    OutboundDataNotAllowed,
    OutboundNotConfigured,
    OutboundService,
)


class PrincipalStore:
    def __init__(self, subject):
        self.principal = PrincipalStatus(subject=subject, role=Role.ARCHITECT, active=True)

    async def find(self, subject):
        return self.principal if subject == self.principal.subject else None


class BusinessService:
    def __init__(self, allowed):
        self.allowed = allowed
        self.actors = []

    async def read_material(self, actor, version_id):
        self.actors.append(actor)
        if version_id != self.allowed:
            raise AccessDenied()
        return b"synthetic material"

    async def read_draft(self, actor, revision_id):
        self.actors.append(actor)
        if revision_id != self.allowed:
            raise AccessDenied()
        return {"schema_version": "1.0", "title": "synthetic draft"}


@pytest.fixture
def boundary():
    subject, allowed = uuid4(), uuid4()
    settings = JwtSettings(
        secret=SecretStr("boundary-signing-key-long-enough-for-tests"),
        issuer="presales-boundary",
        audience="presales-browser",
    )
    store = PrincipalStore(subject)
    service = BusinessService(allowed)
    auth = AuthenticationService(store, settings, ())
    app = create_auth_app(auth, JwtVerifier(settings), business_service=service)
    return app, settings, subject, allowed, service


def test_business_reads_use_verified_actor_and_reject_unknown_sources(boundary):
    app, settings, subject, allowed, service = boundary
    token = JwtIssuer(settings).issue(Actor(subject=subject))
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        material = client.get(f"/api/materials/{allowed}", headers=headers)
        draft = client.get(f"/api/drafts/{allowed}", headers=headers)
        assert material.content == b"synthetic material"
        assert material.headers["cache-control"] == "no-store"
        assert draft.json()["title"] == "synthetic draft"
        assert draft.headers["cache-control"] == "no-store"

        hidden = client.get(f"/api/materials/{uuid4()}", headers=headers)
        unknown = client.get(f"/api/drafts/{uuid4()}", headers=headers)
        assert hidden.status_code == unknown.status_code == 404
        assert hidden.json() == unknown.json() == {"detail": "Not found or not allowed"}
        assert service.actors == [Actor(subject=subject)] * 4


@pytest.mark.parametrize(
    "query", ["user_id=forged", "analysis_attempt_id=forged", "grant=all", "model_url=bad"]
)
def test_business_routes_reject_client_trust_fields(boundary, query):
    app, settings, subject, allowed, service = boundary
    headers = {"Authorization": f"Bearer {JwtIssuer(settings).issue(Actor(subject=subject))}"}
    with TestClient(app) as client:
        for path in (f"/api/materials/{allowed}", f"/api/drafts/{allowed}"):
            response = client.get(f"{path}?{query}", headers=headers)
            assert response.status_code == 422
            assert response.json() == {"detail": "Invalid request"}
            body_response = client.request("GET", path, headers=headers, json={query: "forged"})
            assert body_response.status_code == 422
            assert body_response.json() == {"detail": "Invalid request"}
    assert service.actors == []


def test_authentication_blocks_business_service_before_access(boundary):
    app, settings, subject, allowed, service = boundary
    wrong_audience = settings.model_copy(update={"audience": "other-client"})
    wrong_token = JwtIssuer(wrong_audience).issue(Actor(subject=subject))
    unknown_token = JwtIssuer(settings).issue(Actor(subject=uuid4()))
    with TestClient(app) as client:
        for headers in (
            {},
            {"Authorization": f"Bearer {wrong_token}"},
            {"Authorization": f"Bearer {unknown_token}"},
        ):
            response = client.get(f"/api/materials/{allowed}", headers=headers)
            assert response.status_code == 401
            assert response.json() == {"detail": "Authentication required"}
    assert service.actors == []


def test_default_presales_app_has_no_generic_routes_or_agent_import(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("PRESALES_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRESALES_DATABASE_URL", "postgresql+psycopg://local@localhost/test")
    monkeypatch.setenv("PRESALES_FILE_ROOT", str(tmp_path / "files"))
    monkeypatch.setenv("PRESALES_JWT_SECRET", "boundary-signing-key-long-enough-for-tests")
    monkeypatch.setenv("PRESALES_JWT_ISSUER", "presales-boundary")
    monkeypatch.setenv("PRESALES_JWT_AUDIENCE", "presales-browser")
    for role in ("SALES", "ARCHITECT", "REVIEWER"):
        monkeypatch.setenv(f"PRESALES_DEMO_{role}_USERNAME", role.lower())
        monkeypatch.setenv(f"PRESALES_DEMO_{role}_PASSWORD", "synthetic-local-password")

    app = create_app()
    assert app.state.outbound.describe(OutboundService.OBSERVABILITY).mode == "disabled"
    with TestClient(app) as client:
        assert client.get("/auth/me").status_code == 401
        assert client.get(f"/api/materials/{uuid4()}").status_code == 401
        assert client.get(f"/api/drafts/{uuid4()}").status_code == 401
        for route in (
            "/invoke",
            "/stream",
            "/history",
            "/threads",
            "/agui/run",
            "/feedback",
            "/compat/invoke",
        ):
            assert client.get(route).status_code == 404

    compat = FastAPI()

    @compat.get("/health")
    async def compat_health():
        return {"status": "compat"}

    monkeypatch.setenv("PRESALES_API_COMPATIBILITY_MODE", "true")
    monkeypatch.setenv("PRESALES_API_COMPATIBILITY_CHECKPOINT_PATH", str(tmp_path / "compat.db"))
    monkeypatch.setattr("presales.api.app._load_compatibility_app", lambda _: compat)
    with TestClient(create_app()) as client:
        assert client.get("/compat/health").json() == {"status": "compat"}
        assert client.get("/health").status_code == 404


def test_presales_asgi_imports_without_model_configuration(tmp_path):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PRESALES_")
        and not any(
            fragment in key for fragment in ("API_KEY", "TOKEN", "SECRET", "LANGCHAIN", "LANGFUSE")
        )
    }
    environment.update(
        PYTHONPATH=str(Path("src").resolve()),
        PRESALES_DATABASE_URL="postgresql+psycopg://local@localhost/test",
        PRESALES_FILE_ROOT=str(tmp_path / "files"),
        PRESALES_JWT_SECRET="synthetic-" * 6,
        PRESALES_JWT_ISSUER="presales-boundary",
        PRESALES_JWT_AUDIENCE="presales-browser",
    )
    for role in ("SALES", "ARCHITECT", "REVIEWER"):
        environment[f"PRESALES_DEMO_{role}_USERNAME"] = role.lower()
        environment[f"PRESALES_DEMO_{role}_PASSWORD"] = "synthetic-local-password"
    result = subprocess.run(
        [sys.executable, "-c", "import presales.asgi; assert presales.asgi.app is not None"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_compatibility_requires_explicit_dedicated_checkpoint(tmp_path, monkeypatch):
    assert not PresalesApiSettings().compatibility_mode
    with pytest.raises(ValidationError):
        PresalesApiSettings(compatibility_mode=True)
    with pytest.raises(ValidationError):
        PresalesApiSettings(compatibility_mode=True, compatibility_checkpoint_path="relative.db")

    from core.settings import DatabaseType, settings

    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    with pytest.raises(ValueError, match="AUTH_SECRET"):
        _load_compatibility_app(tmp_path / "compat.db")

    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("generic-test-secret"))
    monkeypatch.setattr(settings, "DATABASE_TYPE", DatabaseType.POSTGRES)
    with pytest.raises(ValueError, match="SQLite"):
        _load_compatibility_app(tmp_path / "compat.db")

    monkeypatch.setattr(settings, "DATABASE_TYPE", DatabaseType.SQLITE)
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "generic.db"))
    with pytest.raises(ValueError, match="separate"):
        _load_compatibility_app(tmp_path / "generic.db")


def test_compatibility_mount_uses_its_own_saver(tmp_path, monkeypatch, boundary):
    import agents
    from core.settings import DatabaseType, settings

    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("generic-test-secret"))
    monkeypatch.setattr(settings, "DATABASE_TYPE", DatabaseType.SQLITE)
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "generic.db"))
    monkeypatch.setattr(settings, "LANGFUSE_TRACING", False)
    monkeypatch.setattr(settings, "LANGCHAIN_TRACING_V2", False)
    agent = SimpleNamespace(checkpointer=None, store=None)
    monkeypatch.setattr(agents, "get_all_agent_info", lambda: [SimpleNamespace(key="fake")])
    monkeypatch.setattr(agents, "get_agent", lambda _: agent)

    async def load_agent(_):
        pass

    monkeypatch.setattr(agents, "load_agent", load_agent)
    compatibility = _load_compatibility_app(tmp_path / "compat.db")
    app, verifier_settings, subject, _, _ = boundary
    auth = AuthenticationService(PrincipalStore(subject), verifier_settings, ())
    mounted = create_auth_app(auth, JwtVerifier(verifier_settings), compatibility_app=compatibility)
    with TestClient(mounted) as client:
        assert agent.checkpointer is not None
        assert client.get("/compat/health").status_code == 200
        assert client.post("/compat/invoke", json={"message": "synthetic"}).status_code == 401
        assert client.get("/invoke").status_code == 404
        assert Path(tmp_path / "compat.db").exists()
        assert settings.SQLITE_DB_PATH == str(tmp_path / "generic.db")


def test_outbound_targets_are_local_and_observation_is_metadata_only(monkeypatch):
    for name in list(os.environ):
        if name.startswith("PRESALES_OUTBOUND_"):
            monkeypatch.delenv(name, raising=False)
    adapter = OutboundAdapter(OutboundSettings())
    for service in OutboundService:
        with pytest.raises(OutboundNotConfigured):
            adapter.resolve(service, "synthetic")

    monkeypatch.setenv("PRESALES_OUTBOUND_EMBEDDING__MODE", "real")
    monkeypatch.setenv("PRESALES_OUTBOUND_EMBEDDING__DESTINATION_ALIAS", "local-test")
    monkeypatch.setenv("PRESALES_OUTBOUND_EMBEDDING__ENDPOINT", "https://example.invalid/v1")
    monkeypatch.setenv("PRESALES_OUTBOUND_EMBEDDING__SERVICE_IDENTITY", "synthetic-model")
    monkeypatch.setenv("PRESALES_OUTBOUND_EMBEDDING__ALLOWED_DATA_CLASSES", '["synthetic"]')
    monkeypatch.setenv("PRESALES_OUTBOUND_EMBEDDING__API_KEY", "synthetic-key")
    adapter = OutboundAdapter(OutboundSettings())
    destination = adapter.resolve(OutboundService.EMBEDDING, "synthetic")
    assert destination.endpoint == "https://example.invalid/v1"
    assert "synthetic-key" not in repr(destination)
    with pytest.raises(OutboundDataNotAllowed):
        adapter.resolve(OutboundService.EMBEDDING, "owned")
    with pytest.raises(OutboundNotConfigured):
        adapter.resolve(OutboundService.OBSERVABILITY, "synthetic")
    with pytest.raises(OutboundNotConfigured):
        adapter.observation("synthetic", {"operation": "retrieve"})
    with pytest.raises(ValidationError):
        OutboundServiceSettings(
            mode="real",
            destination_alias="local-test",
            endpoint="https://example.invalid/v1",
            service_identity="model",
            untrusted_endpoint="https://other.invalid",
        )

    monkeypatch.setenv("PRESALES_OUTBOUND_OBSERVABILITY__MODE", "fake")
    monkeypatch.setenv("PRESALES_OUTBOUND_OBSERVABILITY__ALLOWED_DATA_CLASSES", '["synthetic"]')
    observation = OutboundAdapter(OutboundSettings())
    target, metadata = observation.observation(
        "synthetic", {"operation": "retrieve", "status": "success", "duration_ms": 3}
    )
    assert target.mode == "fake"
    assert metadata == {"operation": "retrieve", "status": "success", "duration_ms": 3}
    for forbidden in ("prompt", "jwt", "evidence", "body", "api_key"):
        with pytest.raises(OutboundDataNotAllowed):
            observation.observation("synthetic", {forbidden: "synthetic-secret"})
