import hmac
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from presales.access.jwt import JwtIssuer
from presales.domain.contracts import Actor, AuthenticationFailed, PrincipalStatus, Role
from presales.infrastructure.config import DemoAccountsSettings, JwtSettings
from presales.maintenance.service import demo_id

"""负责登录和认证的服务类，提供了登录功能，验证用户名和密码，并生成JWT令牌。
"""

# 接口约定：Protocal定义了PrincipalStore的行为规范，要求其必须实现find方法，用于根据用户主体ID查找用户主体状态
class PrincipalStore(Protocol):
    async def find(self, subject: UUID) -> PrincipalStatus | None: ...


# 数据表示：演示账户
@dataclass(frozen=True)
class DemoAccount:
    username: str
    password: str = field(repr=False)
    role: Role


# 数据表示：登录成功的返回结果
@dataclass(frozen=True)
class LoginResult:
    access_token: str = field(repr=False)
    expires_in: int
    token_type: str = "bearer"


# 工厂函数：创建演示账户元组
def demo_accounts(settings: DemoAccountsSettings) -> tuple[DemoAccount, ...]:
    return (
        DemoAccount(
            settings.sales_username, settings.sales_password.get_secret_value(), Role.SALES
        ),
        DemoAccount(
            settings.architect_username,
            settings.architect_password.get_secret_value(),
            Role.ARCHITECT,
        ),
        DemoAccount(
            settings.reviewer_username,
            settings.reviewer_password.get_secret_value(),
            Role.REVIEWER,
        ),
    )

# 认证服务类，执行登录业务流程：验证用户名和密码，检查账户状态，生成JWT令牌
class AuthenticationService:
    def __init__(
        self,
        principal_store: PrincipalStore, # 用户主体存储
        jwt_settings: JwtSettings,      # JWT配置
        accounts: tuple[DemoAccount, ...],      # 演示账户元组
    ) -> None:
        # 将初始化参数赋值给实例变量，保存到当前对象上
        self.principal_store = principal_store
        self.issuer = JwtIssuer(jwt_settings)
        self.accounts = accounts
        self._ttl = jwt_settings.access_token_ttl_seconds

    async def login(self, username: str, password: str) -> LoginResult:
        matched_account = None
        username_bytes = username.encode("utf-8")
        password_bytes = password.encode("utf-8")
        for account in self.accounts:
            username_matches = hmac.compare_digest(account.username.encode("utf-8"), username_bytes)
            password_matches = hmac.compare_digest(account.password.encode("utf-8"), password_bytes)
            if username_matches & password_matches:
                matched_account = account
        if matched_account is None:
            raise AuthenticationFailed()

        subject = demo_id(matched_account.role.value)
        principal = await self.principal_store.find(subject)
        if (
            principal is None
            or not principal.active
            or principal.subject != subject
            or principal.role is not matched_account.role
        ):
            raise AuthenticationFailed()

        token = self.issuer.issue(Actor(subject=subject))
        return LoginResult(access_token=token, expires_in=self._ttl)
