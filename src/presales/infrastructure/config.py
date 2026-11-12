from pathlib import Path
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

"""售前模块的配置管理。定义了数据库、JWT、演示账户和外部服务的配置类，并提供了验证逻辑以确保配置的正确性和安全性。
它从环境变量读取运行参数，并在应用使用这些参数前检查格式和约束。
"""


# 数据库配置类
class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # 为字段对应的环境变量添加前缀，忽略额外字段
        env_prefix="PRESALES_",
        env_file=None,
        extra="ignore",
        hide_input_in_errors=True,
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


# JWT配置类
class JwtSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRESALES_JWT_", env_file=None, extra="ignore", hide_input_in_errors=True
    )
    secret: SecretStr = Field(repr=False)
    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    @field_validator("secret")
    @classmethod
    def strong_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < 32:
            raise ValueError("At least 32 bytes of signing key are required")
        return value


# 演示账号
class DemoAccountsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRESALES_DEMO_",
        env_file=None,
        extra="ignore",
        hide_input_in_errors=True,
    )

    sales_username: str = Field(min_length=1, max_length=128)
    sales_password: SecretStr = Field(repr=False)
    architect_username: str = Field(min_length=1, max_length=128)
    architect_password: SecretStr = Field(repr=False)
    reviewer_username: str = Field(min_length=1, max_length=128)
    reviewer_password: SecretStr = Field(repr=False)

    @field_validator("sales_password", "architect_password", "reviewer_password")
    @classmethod
    def strong_demo_password(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 12:
            raise ValueError("Demo passwords must be at least 12 characters")
        return value

    @model_validator(mode="after")
    def unique_usernames(self):
        usernames = [
            self.sales_username,
            self.architect_username,
            self.reviewer_username,
        ]
        if len(set(usernames)) != len(usernames):
            raise ValueError("Demo usernames must be distinct")
        return self


# 外部服务配置类
class OutboundServiceSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["disabled", "fake", "real"] = "disabled"
    destination_alias: str | None = None
    endpoint: AnyHttpUrl | None = None
    service_identity: str | None = None
    api_key: SecretStr | None = Field(default=None, repr=False)
    allowed_data_classes: frozenset[Literal["synthetic", "public_authorized", "owned"]] = (
        frozenset()
    )

    @model_validator(mode="after")
    def configured_real_service(self):
        if self.mode == "real" and not all(
            [self.destination_alias, self.endpoint, self.service_identity]
        ):
            raise ValueError("Real outbound services require a configured destination and identity")
        return self


# 外部服务总配置类
class OutboundSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRESALES_OUTBOUND_",
        env_nested_delimiter="__",
        env_file=None,
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    language_model: OutboundServiceSettings = Field(default_factory=OutboundServiceSettings)
    embedding: OutboundServiceSettings = Field(default_factory=OutboundServiceSettings)
    reranker: OutboundServiceSettings = Field(default_factory=OutboundServiceSettings)
    judge: OutboundServiceSettings = Field(default_factory=OutboundServiceSettings)
    observability: OutboundServiceSettings = Field(default_factory=OutboundServiceSettings)


class PresalesApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRESALES_API_",
        env_file=None,
        extra="ignore",
        hide_input_in_errors=True,
    )

    compatibility_mode: bool = False
    compatibility_checkpoint_path: Path | None = None

    @model_validator(mode="after")
    def isolated_compatibility_checkpoint(self):
        if self.compatibility_mode and (
            self.compatibility_checkpoint_path is None
            or not self.compatibility_checkpoint_path.is_absolute()
        ):
            raise ValueError(
                "Compatibility mode requires an absolute, dedicated SQLite checkpoint path"
            )
        return self
