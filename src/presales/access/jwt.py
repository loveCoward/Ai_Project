import time
from uuid import UUID

import jwt

from presales.domain.contracts import Actor, AuthenticationFailed
from presales.infrastructure.config import JwtSettings

_CLAIMS = {"sub", "iss", "aud", "iat", "nbf", "exp"}

"""处理身份认证：负责生成和验证JWT令牌，确保请求的合法性。
提供了JwtIssuer类用于签发令牌，以及JwtVerifier类用于验证令牌的有效性和完整性。
"""


class JwtIssuer:
    def __init__(self, settings: JwtSettings) -> None:
        self.settings = settings

    # 根据用户身份信息生成JWT令牌，包含必要的声明和过期时间。
    def issue(self, actor: Actor) -> str:
        now = int(time.time())
        claims = {
            "sub": str(actor.subject),
            "iss": self.settings.issuer,
            "aud": self.settings.audience,
            "iat": now,
            "nbf": now,
            "exp": now + self.settings.access_token_ttl_seconds,
        }
        return jwt.encode(claims, self.settings.secret.get_secret_value(), algorithm="HS256")


class JwtVerifier:
    def __init__(self, settings: JwtSettings) -> None:
        self.settings = settings

    # 验证JWT令牌的有效性，包括签名、声明完整性和过期时间，返回对应的Actor对象。
    def verify(self, token: str) -> Actor:
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise AuthenticationFailed()
        try:
            claims = jwt.decode(
                token,
                self.settings.secret.get_secret_value(),
                algorithms=["HS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["sub", "iss", "aud", "exp", "iat", "nbf"]},
            )
            if set(claims) != _CLAIMS:
                raise jwt.InvalidTokenError
            if not isinstance(claims["sub"], str) or any(
                isinstance(claims[name], bool) or not isinstance(claims[name], int)
                for name in ("iat", "nbf", "exp")
            ):
                raise jwt.InvalidTokenError
            now = int(time.time())
            if (
                claims["iat"] > now
                or claims["nbf"] < claims["iat"]
                or claims["exp"] <= claims["nbf"]
                or claims["exp"] - claims["iat"] > self.settings.access_token_ttl_seconds
            ):
                raise jwt.InvalidTokenError
            subject = UUID(claims["sub"])
            if str(subject) != claims["sub"]:
                raise jwt.InvalidTokenError
            return Actor(subject=subject)
        except (jwt.PyJWTError, OverflowError, ValueError, TypeError, KeyError):
            raise AuthenticationFailed() from None
