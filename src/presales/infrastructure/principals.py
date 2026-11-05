from uuid import UUID

from presales.domain.contracts import PrincipalStatus, Role
from presales.infrastructure.database import Database, rows


class DatabasePrincipalStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def find(self, subject: UUID) -> PrincipalStatus | None:
        async with self.database.transaction() as conn:
            records = await rows(
                conn,
                "SELECT id,role,active FROM iam.principals WHERE id=:id",
                id=subject,
            )
        if not records:
            return None
        return PrincipalStatus(
            subject=records[0]["id"],
            role=Role(records[0]["role"]),
            active=records[0]["active"],
        )
