import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from pydantic import ValidationError

from presales.access.jwt import JwtVerifier
from presales.domain.contracts import (
    AccessDenied,
    AuthenticationFailed,
    DraftContent,
    InvalidInput,
    NotReady,
    digest,
)
from presales.infrastructure.config import DatabaseSettings, JwtSettings
from presales.infrastructure.files import LocalFiles
from presales.maintenance.operator import current_operator, require_operator
from presales.maintenance.service import AuthorizationChange


@pytest.fixture
def token_setup():
    settings = JwtSettings(
        secret="unit-test-signing-key-not-a-real-secret-123", issuer="tests", audience="presales"
    )
    now = int(time.time())
    claims = {
        "sub": str(uuid4()),
        "iss": "tests",
        "aud": "presales",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
    }
    return settings, claims


def test_jwt_rejects_client_supplied_role(token_setup):
    settings, claims = token_setup
    claims["role"] = "administrator"
    with pytest.raises(AuthenticationFailed):
        JwtVerifier(settings).verify(
            jwt.encode(claims, settings.secret.get_secret_value(), algorithm="HS256")
        )


@pytest.mark.parametrize(
    "field,value",
    [("iss", "wrong"), ("aud", "wrong"), ("exp", 1), ("nbf", 9999999999), ("sub", "not-an-id")],
)
def test_jwt_rejects_invalid_claims(token_setup, field, value):
    settings, claims = token_setup
    claims[field] = value
    with pytest.raises(AuthenticationFailed):
        JwtVerifier(settings).verify(
            jwt.encode(claims, settings.secret.get_secret_value(), algorithm="HS256")
        )


@pytest.mark.parametrize("field", ["sub", "iss", "aud", "exp", "iat", "nbf"])
def test_jwt_requires_claims(token_setup, field):
    settings, claims = token_setup
    del claims[field]
    with pytest.raises(AuthenticationFailed):
        JwtVerifier(settings).verify(
            jwt.encode(claims, settings.secret.get_secret_value(), algorithm="HS256")
        )


def test_jwt_rejects_wrong_algorithm_and_signature(token_setup):
    settings, claims = token_setup
    for key, algorithm in [
        ("different-test-key-that-is-long-enough", "HS256"),
        (settings.secret.get_secret_value() * 2, "HS384"),
    ]:
        with pytest.raises(AuthenticationFailed):
            JwtVerifier(settings).verify(jwt.encode(claims, key, algorithm=algorithm))


def test_config_never_loads_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRESALES_DATABASE_URL", raising=False)
    (tmp_path / ".env").write_text("PRESALES_DATABASE_URL=sqlite:///local.db")
    with pytest.raises(ValidationError):
        DatabaseSettings()
    with pytest.raises(ValidationError):
        DatabaseSettings(database_url="sqlite:///local.db")


def test_config_repr_hides_password():
    settings = DatabaseSettings(
        database_url="postgresql+psycopg://test:do-not-display@localhost/test"
    )
    assert "do-not-display" not in repr(settings)


def test_imports_without_models_or_keys(tmp_path):
    environment = {
        k: v
        for k, v in os.environ.items()
        if not any(t in k for t in ["KEY", "TOKEN", "SECRET", "PRESALES"])
    }
    environment["PYTHONPATH"] = str(Path("src").resolve())
    code = "import sys; import presales.application.service; import presales.application.auth; import presales.api.auth; import presales.maintenance.service; assert 'core.settings' not in sys.modules; assert 'langchain' not in sys.modules"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=environment, capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("key", ["../secret", "C:\\secret", "/etc/passwd", "a.bin", "..\\x"])
def test_file_keys_reject_paths(tmp_path, key):
    with pytest.raises(InvalidInput):
        LocalFiles(tmp_path).path(key)


def test_file_write_idempotency_and_corruption(tmp_path):
    storage = LocalFiles(tmp_path)
    key = f"{uuid4().hex}.bin"
    data = b"synthetic customer file"
    checksum = hashlib.sha256(data).hexdigest()
    storage.write(key, data, checksum)
    storage.write(key, data, checksum)
    assert storage.read(key, checksum, len(data)) == data
    (tmp_path / key).write_bytes(b"corrupt")
    with pytest.raises(NotReady):
        storage.read(key, checksum, len(data))


def test_file_missing_and_digest_mismatch(tmp_path):
    storage = LocalFiles(tmp_path)
    key = f"{uuid4().hex}.bin"
    with pytest.raises(NotReady):
        storage.read(key, "0" * 64, 1)
    with pytest.raises(InvalidInput):
        storage.write(key, b"test", "0" * 64)


def test_file_symlink_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not grant symlink creation; Windows reparse test runs separately")
    with pytest.raises(InvalidInput):
        LocalFiles(link)


def test_contract_rejects_forged_identity():
    with pytest.raises(ValidationError):
        DraftContent(title="Synthetic draft", identity={"approved": True})
    with pytest.raises(ValidationError):
        AuthorizationChange(kind="grant", principal_id=uuid4(), role="reviewer")


def test_canonical_digest_is_stable_and_sensitive():
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})
    assert digest({"b": 2}) != digest({"b": 3})


def test_invalid_configuration_errors_do_not_include_raw_secrets():
    secret = "synthetic-invalid-connection-secret"
    with pytest.raises(ValidationError) as error:
        DatabaseSettings(database_url="sqlite:///" + secret)
    assert secret not in str(error.value)


def test_operator_is_os_identity_not_user_supplied_actor():
    with pytest.raises(AccessDenied):
        require_operator({"not-the-current-os-identity"})
    identity = current_operator()
    assert require_operator({identity}) == identity


def test_draft_machine_schema_has_explicit_version():
    schema = DraftContent.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == "1.0"
    saved = json.loads(
        Path("docs/contracts/phase-one-draft-input.schema.json").read_text(encoding="utf-8")
    )
    assert saved == schema


@pytest.mark.skipif(os.name != "nt", reason="Windows junction check")
def test_windows_junction_root_is_rejected(tmp_path):
    target = tmp_path / "target"
    link = tmp_path / "junction"
    target.mkdir()
    command = "New-Item -ItemType Junction -Path $env:PRESALES_TEST_LINK -Target $env:PRESALES_TEST_TARGET | Out-Null"
    environment = os.environ.copy()
    environment.update(PRESALES_TEST_LINK=str(link), PRESALES_TEST_TARGET=str(target))
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        env=environment,
        timeout=10,
    )
    assert result.returncode == 0
    try:
        with pytest.raises(InvalidInput):
            LocalFiles(link)
    finally:
        link.rmdir()
