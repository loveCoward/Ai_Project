from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from presales.application.service import audit
from presales.domain.contracts import (
    AccessDenied,
    Conflict,
    DomainError,
    InvalidInput,
    Role,
    canonical,
    digest,
)
from presales.infrastructure.database import AUTH_LOCK, Database, execute, lock, rows
from presales.maintenance.operator import require_operator

NAMESPACE = UUID("5ca8b865-cc4c-4d65-8de5-7bfe9b60ca08")


def demo_id(name: str) -> UUID:
    return uuid5(NAMESPACE, name)


class AuthorizationChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["grant", "revoke", "active", "role"]
    principal_id: UUID
    resource_id: UUID | None = None
    active: bool | None = None
    role: Role | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.kind in {"grant", "revoke"}:
            valid = self.resource_id is not None and self.active is None and self.role is None
        elif self.kind == "active":
            valid = self.active is not None and self.resource_id is None and self.role is None
        else:
            valid = self.role is not None and self.resource_id is None and self.active is None
        if not valid:
            raise ValueError("Fields do not match authorization change kind")
        return self

"""初始化演示数据、预览和应用授权变更的维护服务。
提供了数据库事务管理、锁定机制、授权变更验证和审计功能。
"""
class Maintenance:
    def __init__(self, database: Database, allowlist: set[str]) -> None:
        self.database = database
        self.allowlist = allowlist

    async def initialize_demo(self) -> dict:
        operator = require_operator(self.allowlist)
        async with self.database.transaction() as conn:
            await lock(conn, AUTH_LOCK)
            if await rows(conn, "SELECT 1 FROM iam.initialization"):
                return {"status": "already_initialized"}
            for name in ["sales", "architect", "reviewer"]:
                await execute(
                    conn,
                    "INSERT INTO iam.principals(id,display_name,role) VALUES(:id,:name,:role)",
                    id=demo_id(name),
                    name=f"Synthetic {name}",
                    role=name,
                )
            for name in ["opportunity-a", "opportunity-b"]:
                identifier = demo_id(name)
                await execute(
                    conn,
                    "INSERT INTO presales.resources VALUES(:id,'opportunity',NULL)",
                    id=identifier,
                )
                await execute(
                    conn,
                    "INSERT INTO presales.opportunities(id,name,created_by) VALUES(:id,:name,:actor)",
                    id=identifier,
                    name=f"Synthetic {name}",
                    actor=demo_id("architect"),
                )
            for name in ["sales", "architect", "reviewer"]:
                await execute(
                    conn,
                    "INSERT INTO iam.resource_grants VALUES(:actor,:resource)",
                    actor=demo_id(name),
                    resource=demo_id("opportunity-a"),
                )
            await execute(conn, "UPDATE iam.authorization_revision SET revision=revision+1")
            await execute(
                conn,
                "INSERT INTO iam.initialization(singleton,operator) VALUES(true,:operator)",
                operator=operator,
            )
            revision = (await rows(conn, "SELECT revision FROM iam.authorization_revision"))[0][
                "revision"
            ]
            await audit(conn, operator, "initialize_demo", None, "success", revision)
            return {
                "status": "initialized",
                "principals": {
                    name: str(demo_id(name)) for name in ["sales", "architect", "reviewer"]
                },
                "opportunity_id": str(demo_id("opportunity-a")),
            }

    async def preview(self, changes: list[AuthorizationChange]) -> dict:
        operator = require_operator(self.allowlist)
        if not changes or len(changes) > 100:
            raise InvalidInput()
        values = [change.model_dump(mode="json") for change in changes]
        keys = [
            (c.principal_id, c.resource_id, "grant" if c.kind in {"grant", "revoke"} else c.kind)
            for c in changes
        ]
        if len(set(keys)) != len(keys):
            raise InvalidInput()
        async with self.database.transaction() as conn:
            await lock(conn, AUTH_LOCK, shared=True)
            revision = (await rows(conn, "SELECT revision FROM iam.authorization_revision"))[0][
                "revision"
            ]
            before, expansion = [], False
            for change in changes:
                principals = await rows(
                    conn,
                    "SELECT role,active FROM iam.principals WHERE id=:id",
                    id=change.principal_id,
                )
                if not principals:
                    raise AccessDenied()
                old = principals[0]
                if change.resource_id:
                    resource = await rows(
                        conn,
                        "SELECT kind FROM presales.resources WHERE id=:id",
                        id=change.resource_id,
                    )
                    if not resource:
                        raise AccessDenied()
                    if resource[0]["kind"] not in {"opportunity", "task", "knowledge"}:
                        raise AccessDenied()
                    grants = await rows(
                        conn,
                        "SELECT 1 FROM iam.resource_grants WHERE principal_id=:actor AND resource_id=:resource",
                        actor=change.principal_id,
                        resource=change.resource_id,
                    )
                    old = {"granted": bool(grants)}
                expands = (
                    (change.kind == "grant" and not old.get("granted"))
                    or (change.kind == "role" and change.role != old.get("role"))
                    or (change.kind == "active" and change.active is True and not old.get("active"))
                )
                expansion = expansion or expands
                before.append(old)
            identifier = uuid4()
            fingerprint = digest({"revision": revision, "changes": values, "before": before})
            await execute(
                conn,
                """INSERT INTO iam.authorization_changes
                (id,operator,base_revision,changes,before_values,digest,expansion)
                VALUES(:id,:operator,:revision,CAST(:changes AS jsonb),CAST(:before AS jsonb),:digest,:expansion)""",
                id=identifier,
                operator=operator,
                revision=revision,
                changes=canonical(values),
                before=canonical(before),
                digest=fingerprint,
                expansion=expansion,
            )
            await audit(conn, operator, "authorization_preview", identifier, "success", revision)
            return {
                "change_id": str(identifier),
                "base_revision": revision,
                "changes": values,
                "before": before,
                "expansion": expansion,
                "digest": fingerprint,
            }

    async def apply(
        self, change_id: UUID, expected_digest: str, reason: str, *, confirm_expansion: bool = False
    ) -> dict:
        operator = require_operator(self.allowlist)
        try:
            return await self._apply(
                change_id, expected_digest, reason, confirm_expansion=confirm_expansion
            )
        except DomainError as error:
            async with self.database.transaction() as conn:
                revision = (await rows(conn, "SELECT revision FROM iam.authorization_revision"))[0][
                    "revision"
                ]
                await audit(conn, operator, "authorization_apply", change_id, error.code, revision)
            raise

    async def _apply(
        self, change_id: UUID, expected_digest: str, reason: str, *, confirm_expansion: bool = False
    ) -> dict:
        operator = require_operator(self.allowlist)
        if not reason.strip() or len(reason) > 500:
            raise InvalidInput()
        async with self.database.transaction() as conn:
            await lock(conn, AUTH_LOCK)
            values = await rows(
                conn,
                "SELECT * FROM iam.authorization_changes WHERE id=:id FOR UPDATE",
                id=change_id,
            )
            if not values or values[0]["operator"] != operator:
                raise AccessDenied()
            record = values[0]
            fingerprint = digest(
                {
                    "revision": record["base_revision"],
                    "changes": record["changes"],
                    "before": record["before_values"],
                }
            )
            if record["digest"] != expected_digest or fingerprint != expected_digest:
                raise Conflict()
            if record["applied_revision"] is not None:
                if record["reason"] != reason:
                    raise Conflict()
                return {"change_id": str(change_id), "revision": record["applied_revision"]}
            revision = (
                await rows(conn, "SELECT revision FROM iam.authorization_revision FOR UPDATE")
            )[0]["revision"]
            if record["base_revision"] != revision:
                raise Conflict()
            if record["expansion"] and not confirm_expansion:
                raise AccessDenied()
            for item in record["changes"]:
                change = AuthorizationChange.model_validate(item)
                if change.kind == "grant":
                    await execute(
                        conn,
                        "INSERT INTO iam.resource_grants VALUES(:actor,:resource) ON CONFLICT DO NOTHING",
                        actor=change.principal_id,
                        resource=change.resource_id,
                    )
                elif change.kind == "revoke":
                    await execute(
                        conn,
                        "DELETE FROM iam.resource_grants WHERE principal_id=:actor AND resource_id=:resource",
                        actor=change.principal_id,
                        resource=change.resource_id,
                    )
                elif change.kind == "active":
                    await execute(
                        conn,
                        "UPDATE iam.principals SET active=:active WHERE id=:id",
                        active=change.active,
                        id=change.principal_id,
                    )
                else:
                    await execute(
                        conn,
                        "UPDATE iam.principals SET role=:role WHERE id=:id",
                        role=change.role,
                        id=change.principal_id,
                    )
            await execute(conn, "UPDATE iam.authorization_revision SET revision=revision+1")
            await execute(
                conn,
                "UPDATE iam.authorization_changes SET applied_revision=:revision,reason=:reason WHERE id=:id",
                id=change_id,
                revision=revision + 1,
                reason=reason,
            )
            await audit(conn, operator, "authorization_apply", change_id, "success", revision + 1)
            return {"change_id": str(change_id), "revision": revision + 1}

    async def status(self, change_id: UUID) -> dict:
        operator = require_operator(self.allowlist)
        async with self.database.transaction() as conn:
            records = await rows(
                conn,
                "SELECT id,base_revision,applied_revision,digest FROM iam.authorization_changes WHERE id=:id AND operator=:operator",
                id=change_id,
                operator=operator,
            )
            if not records:
                raise AccessDenied()
            return records[0]
