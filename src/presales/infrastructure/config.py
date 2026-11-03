from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRESALES_", env_file=None, extra="ignore", hide_input_in_errors=True
    )
    database_url: SecretStr
    pool_size: int = Field(default=3, ge=1, le=20)
    lock_timeout_ms: int = Field(default=5000, ge=1, le=60_000)
    statement_timeout_ms: int = Field(default=15_000, ge=1, le=120_000)
    file_root: Path = Path(".presales-data")

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            if url.drivername != "postgresql+psycopg" or not url.database:
                raise ValueError
        except Exception:
            raise ValueError("An explicit PostgreSQL psycopg database URL is required") from None
        return value


class JwtSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRESALES_JWT_", env_file=None, extra="ignore", hide_input_in_errors=True
    )
    secret: SecretStr = Field(repr=False)
    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)

    @field_validator("secret")
    @classmethod
    def strong_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < 32:
            raise ValueError("At least 32 bytes of signing key are required")
        return value
