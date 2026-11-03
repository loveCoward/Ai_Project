from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from presales.domain.contracts import AccessDenied, Actor, Role
from presales.infrastructure.database import AUTH_LOCK, lock, rows


class Policy:
    def __init__(self, conn: AsyncConnection, actor: Actor) -> None:
        self.conn = conn
        self.actor = actor
        self.role = ""
        self.authorization_revision = 0

    async def start(self, *, architect: bool = False) -> None:
        await lock(self.conn, AUTH_LOCK, shared=True)
        principals = await rows(
            self.conn, "SELECT role,active FROM iam.principals WHERE id=:id", id=self.actor.subject
        )
        if not principals or not principals[0]["active"]:
            raise AccessDenied()
        self.role = principals[0]["role"]
        if architect and self.role != Role.ARCHITECT:
            raise AccessDenied()
        revision = await rows(self.conn, "SELECT revision FROM iam.authorization_revision")
        self.authorization_revision = revision[0]["revision"]

    async def resource(self, resource_id: UUID) -> dict:
        visited: set[UUID] = set()
        current: UUID | None = resource_id
        original = None
        allowed = False
        while current is not None:
            if current in visited:
                raise AccessDenied()
            visited.add(current)
            values = await rows(
                self.conn, "SELECT * FROM presales.resources WHERE id=:id", id=current
            )
            if not values:
                raise AccessDenied()
            resource = values[0]
            original = original or resource
            grants = await rows(
                self.conn,
                "SELECT 1 FROM iam.resource_grants WHERE principal_id=:actor AND resource_id=:id",
                actor=self.actor.subject,
                id=current,
            )
            allowed = allowed or bool(grants)
            current = resource["parent_id"]
        if not allowed or original is None:
            raise AccessDenied()
        return original

    async def source_closure(self, source_id: UUID) -> set[UUID]:
        active: set[UUID] = set()
        complete: set[UUID] = set()
        stack = [(source_id, False)]
        while stack:
            current, exiting = stack.pop()
            if exiting:
                active.remove(current)
                complete.add(current)
                continue
            if current in active:
                raise AccessDenied()
            if current in complete:
                continue
            values = await rows(
                self.conn, "SELECT * FROM presales.resource_versions WHERE id=:id", id=current
            )
            if not values:
                raise AccessDenied()
            version = values[0]
            await self.resource(version["resource_id"])
            if not version["ready"] or not version["dependencies_complete"]:
                raise AccessDenied()
            readiness = await rows(
                self.conn,
                """SELECT f.state FROM presales.material_versions m
                   LEFT JOIN presales.file_readiness f ON f.id=m.id WHERE m.id=:id""",
                id=current,
            )
            if readiness and readiness[0]["state"] != "ready":
                raise AccessDenied()
            dependencies = await rows(
                self.conn,
                "SELECT upstream_id FROM presales.direct_dependencies WHERE downstream_id=:id",
                id=current,
            )
            actual = {row["upstream_id"] for row in dependencies}
            try:
                expected = {UUID(item) for item in version["input_manifest"]}
            except (ValueError, TypeError):
                raise AccessDenied() from None
            if actual != expected:
                raise AccessDenied()
            if version["source_kind"] == "original":
                if actual or version["declaration"] not in {
                    "synthetic",
                    "owned",
                    "public_authorized",
                }:
                    raise AccessDenied()
            elif not actual:
                raise AccessDenied()
            active.add(current)
            stack.append((current, True))
            stack.extend((item, False) for item in sorted(actual, key=str))
        return complete
