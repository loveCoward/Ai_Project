import os
import time
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from presales.access.jwt import JwtIssuer, JwtVerifier
from presales.api.app import create_app
from presales.api.auth import LoginRequest, LoginResponse, create_auth_app
from presales.application.auth import AuthenticationService, demo_accounts
from presales.domain.contracts import Actor, AuthenticationFailed, PrincipalStatus, Role
from presales.infrastructure.config import (
    DemoAccountsSettings,
    JwtSettings,
    OutboundServiceSettings,
    OutboundSettings,
)
from presales.maintenance.service import demo_id


@pytest.fixture
def jwt_settings():
    return JwtSettings(
        secret=SecretStr("authentication-test-signing-key-long-enough"),
        issuer="presales-test",
        audience="presales-test-client",
        access_token_ttl_seconds=300,
    )


@pytest.fixture
def account_settings():
    return DemoAccountsSettings(
        sales_username="sales-user",
        sales_password=SecretStr("sales-test-password"),
        architect_username="architect-user",
        architect_password=SecretStr("architect-test-password"),
        reviewer_username="reviewer-user",
        reviewer_password=SecretStr("reviewer-test-password"),
    )


class PrincipalStoreStub:
    def __init__(self):
        self.principals = {
            demo_id(role.value): PrincipalStatus(
                subject=demo_id(role.value), role=role, active=True
            )
            for role in Role
        }

    async def find(self, subject: UUID) -> PrincipalStatus | None:
        return self.principals.get(subject)


@pytest.fixture
def auth_components(jwt_settings, account_settings):
    service = AuthenticationService(
        PrincipalStoreStub(), jwt_settings, demo_accounts(account_settings)
    )
    verifier = JwtVerifier(jwt_settings)
    return service, verifier


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "password", "role"),
    [
        ("sales-user", "sales-test-password", Role.SALES),
        ("architect-user", "architect-test-password", Role.ARCHITECT),
        ("reviewer-user", "reviewer-test-password", Role.REVIEWER),
    ],
)
async def test_demo_login_issues_identity_only_token(
    username, password, role, auth_components, jwt_settings
):
    service, verifier = auth_components
    result = await service.login(username, password)
    actor = verifier.verify(result.access_token)
    claims = jwt.decode(
        result.access_token,
        jwt_settings.secret.get_secret_value(),
        algorithms=["HS256"],
        audience=jwt_settings.audience,
        issuer=jwt_settings.issuer,
    )

    assert actor == Actor(subject=demo_id(role.value))
    assert result.expires_in == jwt_settings.access_token_ttl_seconds
    assert password not in repr(result)
    assert set(claims) == {"sub", "iss", "aud", "iat", "nbf", "exp"}
    assert not {"password", "role", "permissions", "grants"}.intersection(claims)


@pytest.mark.asyncio
async def test_login_rejects_wrong_credentials_and_inactive_principal(
    account_settings, jwt_settings
):
    store = PrincipalStoreStub()
    service = AuthenticationService(store, jwt_settings, demo_accounts(account_settings))

    for username, password in [
        ("sales-user", "wrong-password"),
        ("unknown-user", "sales-test-password"),
    ]:
        with pytest.raises(AuthenticationFailed):
            await service.login(username, password)

    store.principals[demo_id("sales")] = PrincipalStatus(
        subject=demo_id("sales"), role=Role.SALES, active=False
    )
    with pytest.raises(AuthenticationFailed):
        await service.login("sales-user", "sales-test-password")


@pytest.mark.asyncio
async def test_login_rejects_role_mismatch(account_settings, jwt_settings):
    store = PrincipalStoreStub()
    store.principals[demo_id("sales")] = PrincipalStatus(
        subject=demo_id("sales"), role=Role.REVIEWER, active=True
    )
    service = AuthenticationService(store, jwt_settings, demo_accounts(account_settings))

    with pytest.raises(AuthenticationFailed):
        await service.login("sales-user", "sales-test-password")


def test_issuer_bounds_token_lifetime_and_verifier_rejects_extra_claims(jwt_settings):
    actor = Actor(subject=demo_id("architect"))
    token = JwtIssuer(jwt_settings).issue(actor)
    claims = jwt.decode(
        token,
        jwt_settings.secret.get_secret_value(),
        algorithms=["HS256"],
        audience=jwt_settings.audience,
        issuer=jwt_settings.issuer,
    )
    assert claims["exp"] - claims["iat"] == jwt_settings.access_token_ttl_seconds
    assert JwtVerifier(jwt_settings).verify(token) == actor

    for injected_claim in ({"role": "reviewer"}, {"resource_grants": ["all"]}):
        forged = {**claims, **injected_claim}
        forged_token = jwt.encode(forged, jwt_settings.secret.get_secret_value(), algorithm="HS256")
        with pytest.raises(AuthenticationFailed):
            JwtVerifier(jwt_settings).verify(forged_token)

    invalid_time_order = dict(claims, nbf=claims["iat"] - 1)
    invalid_time_order_token = jwt.encode(
        invalid_time_order, jwt_settings.secret.get_secret_value(), algorithm="HS256"
    )
    with pytest.raises(AuthenticationFailed):
        JwtVerifier(jwt_settings).verify(invalid_time_order_token)

    oversized = dict(claims, exp=claims["iat"] + jwt_settings.access_token_ttl_seconds + 1)
    oversized_token = jwt.encode(
        oversized, jwt_settings.secret.get_secret_value(), algorithm="HS256"
    )
    with pytest.raises(AuthenticationFailed):
        JwtVerifier(jwt_settings).verify(oversized_token)


def test_verifier_rejects_expired_token(jwt_settings):
    now = int(time.time())
    expired = {
        "sub": str(demo_id("sales")),
        "iss": jwt_settings.issuer,
        "aud": jwt_settings.audience,
        "iat": now - 120,
        "nbf": now - 120,
        "exp": now - 1,
    }
    token = jwt.encode(expired, jwt_settings.secret.get_secret_value(), algorithm="HS256")
    with pytest.raises(AuthenticationFailed):
        JwtVerifier(jwt_settings).verify(token)


def test_login_and_identity_routes_hide_credentials(auth_components):
    service, verifier = auth_components
    app = create_auth_app(service, verifier)
    with TestClient(app) as client:
        failed = client.post(
            "/auth/login",
            json={"username": "sales-user", "password": "wrong-password"},
        )
        assert failed.status_code == 401
        assert "wrong-password" not in failed.text

        extra = client.post(
            "/auth/login",
            json={"username": "sales-user", "password": "sales-test-password", "role": "reviewer"},
        )
        assert extra.status_code == 422
        assert "sales-test-password" not in extra.text

        token_response = client.post(
            "/auth/login",
            json={"username": "sales-user", "password": "sales-test-password"},
        )
        assert token_response.status_code == 200
        assert token_response.headers["cache-control"] == "no-store"
        token = token_response.json()["access_token"]
        identity = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert identity.status_code == 200
        assert identity.headers["cache-control"] == "no-store"
        assert identity.json() == {"subject": str(demo_id("sales"))}
        assert client.get("/auth/me").status_code == 401
        service.principal_store.principals[demo_id("sales")] = PrincipalStatus(
            subject=demo_id("sales"), role=Role.SALES, active=False
        )
        assert (
            client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        )


def test_presales_app_fails_closed_without_configuration(monkeypatch):
    for name in list(os.environ):
        if name.startswith("PRESALES_"):
            monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        create_app()


def test_demo_credentials_and_secrets_are_validated_and_hidden():
    with pytest.raises(ValidationError):
        DemoAccountsSettings(
            sales_username="same",
            sales_password="short",
            architect_username="same",
            architect_password="architect-test-password",
            reviewer_username="reviewer",
            reviewer_password="reviewer-test-password",
        )
    settings = DemoAccountsSettings(
        sales_username="sales-user",
        sales_password=SecretStr("sales-test-password"),
        architect_username="architect-user",
        architect_password=SecretStr("architect-test-password"),
        reviewer_username="reviewer-user",
        reviewer_password=SecretStr("reviewer-test-password"),
    )
    assert "sales-test-password" not in repr(settings)
    assert "sales-test-password" not in repr(
        LoginRequest(username="sales-user", password="sales-test-password")
    )
    assert "token-value" not in repr(
        LoginResponse(access_token="token-value", token_type="bearer", expires_in=300)
    )


def test_outbound_services_are_disabled_without_destination_configuration():
    settings = OutboundSettings()
    assert all(
        service.mode == "disabled"
        for service in (
            settings.language_model,
            settings.embedding,
            settings.reranker,
            settings.judge,
            settings.observability,
        )
    )
    with pytest.raises(ValidationError):
        OutboundServiceSettings(mode="real")


def test_auth_secrets_are_not_loaded_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith(("PRESALES_JWT_", "PRESALES_DEMO_")):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "PRESALES_JWT_SECRET=not-a-runtime-secret\n"
        "PRESALES_JWT_ISSUER=dotenv\n"
        "PRESALES_JWT_AUDIENCE=dotenv\n"
    )

    with pytest.raises(ValidationError):
        JwtSettings()
    with pytest.raises(ValidationError):
        DemoAccountsSettings()
