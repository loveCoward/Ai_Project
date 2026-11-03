from uuid import UUID

import jwt

from presales.domain.contracts import Actor, AuthenticationFailed
from presales.infrastructure.config import JwtSettings


class JwtVerifier:
    def __init__(self, settings: JwtSettings) -> None:
        self.settings = settings

    def verify(self, token: str) -> Actor:
        try:
            claims = jwt.decode(
                token,
                self.settings.secret.get_secret_value(),
                algorithms=["HS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["sub", "iss", "aud", "exp", "iat", "nbf"]},
            )
            return Actor(subject=UUID(claims["sub"]))
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
            raise AuthenticationFailed() from None
