import hashlib
from uuid import UUID, uuid4

from presales.access.policy import Policy
from presales.application.service import PresalesService, audit
from presales.domain.contracts import (
    AccessDenied,
    Actor,
    Conflict,
    InvalidInput,
    NotReady,
    SourceDeclaration,
    canonical,
    digest,
)
from presales.infrastructure.database import execute, lock, rows
from presales.knowledge.models import CATALOG_LOCK, MAX_VERSIONS, Applicability


def scope_of(version: dict) -> Applicability:
    return Applicability(
        products=tuple(version["product_scope"]), deployments=tuple(version["deployment_scope"])
    )


class KnowledgeRegistry:
    def __init__(self, service: PresalesService) -> None:
        self.service = service
        self.database = service.database

    async def _receipt(
        self, conn, actor: Actor, operation: str, request_id: UUID, fingerprint: str
    ):
        await lock(conn, int(digest([actor.subject, operation, request_id])[:7], 16) + 100)
        receipts = await rows(
            conn,
            "SELECT * FROM presales.command_receipts WHERE actor=:actor AND operation=:op AND request_id=:id",
            actor=actor.subject,
            op=operation,
            id=request_id,
        )
        if receipts:
            if receipts[0]["parameter_digest"] != fingerprint:
                raise Conflict()
            return receipts[0]["result"]
        return None

    async def _save_receipt(
        self, conn, actor: Actor, operation: str, request_id: UUID, fingerprint: str, result: dict
    ) -> None:
        await execute(
            conn,
            """INSERT INTO presales.command_receipts(actor,operation,request_id,parameter_digest,result)
            VALUES(:actor,:op,:id,:fingerprint,CAST(:result AS jsonb))""",
            actor=actor.subject,
            op=operation,
            id=request_id,
            fingerprint=fingerprint,
            result=canonical(result),
        )

    async def create_document(self, actor: Actor, title: str, request_id: UUID) -> dict:
        if not title.strip() or len(title) > 500:
            raise InvalidInput()
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            fingerprint = digest({"title": title})
            previous = await self._receipt(
                conn, actor, "create_knowledge_document", request_id, fingerprint
            )
            if previous:
                await policy.resource(UUID(previous["document_id"]))
                return previous
            identifier = uuid4()
            await execute(
                conn, "INSERT INTO presales.resources VALUES(:id,'knowledge',NULL)", id=identifier
            )
            await execute(
                conn,
                "INSERT INTO presales.knowledge_documents(id,title,created_by) VALUES(:id,:title,:actor)",
                id=identifier,
                title=title,
                actor=actor.subject,
            )
            result = {"document_id": str(identifier)}
            await self._save_receipt(
                conn, actor, "create_knowledge_document", request_id, fingerprint, result
            )
            await audit(
                conn,
                str(actor.subject),
                "create_knowledge_document",
                identifier,
                "success",
                policy.authorization_revision,
                request_id,
            )
            return result

    async def register_version(
        self,
        actor: Actor,
        document_id: UUID,
        filename: str,
        data: bytes,
        declaration: SourceDeclaration,
        scope: Applicability,
        request_id: UUID,
    ) -> dict:
        if (
            declaration
            not in {
                SourceDeclaration.SYNTHETIC,
                SourceDeclaration.OWNED,
                SourceDeclaration.PUBLIC_AUTHORIZED,
            }
            or not filename
            or len(filename) > 500
            or len(data) > 20 * 1024 * 1024
        ):
            raise InvalidInput()
        sha = hashlib.sha256(data).hexdigest()
        fingerprint = digest([document_id, filename, sha, declaration, scope.model_dump()])
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            await policy.resource(document_id)
            if not await rows(
                conn, "SELECT id FROM presales.knowledge_documents WHERE id=:id", id=document_id
            ):
                raise AccessDenied()
            previous = await self._receipt(
                conn, actor, "register_knowledge_version", request_id, fingerprint
            )
            if previous:
                identifier = UUID(previous["version_id"])
                result = previous
            else:
                await lock(conn, CATALOG_LOCK)
                count = (
                    await rows(conn, "SELECT count(*) AS total FROM presales.knowledge_versions")
                )[0]["total"]
                if count >= MAX_VERSIONS:
                    raise InvalidInput()
                identifier = uuid4()
                number = (
                    await rows(
                        conn,
                        "SELECT coalesce(max(version_no),0)+1 AS number FROM presales.resource_versions WHERE resource_id=:id",
                        id=document_id,
                    )
                )[0]["number"]
                await execute(
                    conn,
                    """INSERT INTO presales.resource_versions
                    (id,resource_id,version_no,source_kind,declaration,created_by)
                    VALUES(:id,:document,:number,'original',:declaration,:actor)""",
                    id=identifier,
                    document=document_id,
                    number=number,
                    declaration=declaration.value,
                    actor=actor.subject,
                )
                await execute(
                    conn,
                    """INSERT INTO presales.knowledge_versions
                    (id,document_id,filename,storage_key,sha256,size_bytes,product_scope,deployment_scope,declaration,submitted_by)
                    VALUES(:id,:document,:filename,:key,:sha,:size,CAST(:products AS jsonb),CAST(:deployments AS jsonb),:declaration,:actor)""",
                    id=identifier,
                    document=document_id,
                    filename=filename,
                    key=f"{identifier.hex}.bin",
                    sha=sha,
                    size=len(data),
                    products=canonical(scope.products),
                    deployments=canonical(scope.deployments),
                    declaration=declaration.value,
                    actor=actor.subject,
                )
                result = {"version_id": str(identifier), "document_id": str(document_id)}
                await self._save_receipt(
                    conn, actor, "register_knowledge_version", request_id, fingerprint, result
                )
                await audit(
                    conn,
                    str(actor.subject),
                    "register_knowledge_version",
                    identifier,
                    "success",
                    policy.authorization_revision,
                    request_id,
                )
        self.service.files.write(f"{identifier.hex}.bin", data, sha)
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            await policy.resource(document_id)
            self.service.files.read(f"{identifier.hex}.bin", sha, len(data))
            await execute(
                conn,
                "UPDATE presales.resource_versions SET ready=true,dependencies_complete=true WHERE id=:id AND NOT ready",
                id=identifier,
            )
            await execute(
                conn,
                "UPDATE presales.knowledge_versions SET ready=true WHERE id=:id AND NOT ready",
                id=identifier,
            )
        return result

    async def version(self, conn, policy: Policy, version_id: UUID, *, ready: bool = True) -> dict:
        versions = await rows(
            conn, "SELECT * FROM presales.knowledge_versions WHERE id=:id", id=version_id
        )
        if not versions:
            raise AccessDenied()
        version = versions[0]
        await policy.resource(version["document_id"])
        if ready:
            if not version["ready"]:
                raise NotReady()
            await policy.source_closure(version_id)
            self.service.files.read(
                version["storage_key"], version["sha256"], version["size_bytes"]
            )
        return version

    async def inspect(self, actor: Actor, version_id: UUID) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            version = await self.version(conn, policy, version_id, ready=False)
            others = await rows(
                conn,
                "SELECT * FROM presales.knowledge_versions WHERE document_id=:document AND id<>:id AND state IN ('candidate','awaiting_review','published')",
                document=version["document_id"],
                id=version_id,
            )
            return {
                "version_id": str(version_id),
                "state": version["state"],
                "revision": version["revision"],
                "catalog_revision": (
                    await rows(conn, "SELECT revision FROM presales.knowledge_catalog")
                )[0]["revision"],
                "ready": version["ready"],
                "scope": scope_of(version).model_dump(),
                "conflicts": [
                    str(v["id"]) for v in others if scope_of(version).overlaps(scope_of(v))
                ],
            }

    async def published(self, actor: Actor, scope: Applicability) -> list[dict]:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            await lock(conn, CATALOG_LOCK, shared=True)
            versions = await rows(
                conn,
                """SELECT k.*,d.build_id FROM presales.knowledge_directory d
                JOIN presales.knowledge_versions k ON k.id=d.version_id WHERE k.state='published'""",
            )
            visible = []
            for version in versions:
                if not scope.overlaps(scope_of(version)):
                    continue
                try:
                    await self.version(conn, policy, version["id"])
                except (AccessDenied, NotReady):
                    continue
                conflicts = await rows(
                    conn,
                    "SELECT * FROM presales.knowledge_versions WHERE document_id=:document AND id<>:id AND state IN ('candidate','awaiting_review')",
                    document=version["document_id"],
                    id=version["id"],
                )
                if any(
                    scope_of(v).overlaps(scope_of(version)) and scope.overlaps(scope_of(v))
                    for v in conflicts
                ):
                    raise Conflict()
                visible.append(
                    {"version_id": str(version["id"]), "build_id": str(version["build_id"])}
                )
            return visible
