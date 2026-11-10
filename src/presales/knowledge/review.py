from uuid import UUID, uuid4

from presales.access.policy import Policy
from presales.application.service import audit
from presales.domain.contracts import (
    AccessDenied,
    Actor,
    Conflict,
    InvalidInput,
    NotReady,
    Role,
    canonical,
    digest,
)
from presales.infrastructure.database import execute, lock, rows
from presales.knowledge.models import (
    CATALOG_LOCK,
    MAX_CHUNKS,
    MAX_VERSIONS,
    Applicability,
    KnowledgeAction,
)
from presales.knowledge.registry import KnowledgeRegistry, scope_of


class KnowledgeReview:
    def __init__(self, registry: KnowledgeRegistry) -> None:
        self.registry = registry
        self.database = registry.database

    async def _complete_build(self, conn, version_id: UUID, build_id: UUID) -> dict:
        builds = await rows(
            conn,
            """SELECT b.*,
            (SELECT count(*) FROM presales.chunks c WHERE c.build_id=b.id) AS actual_chunks,
            (SELECT count(*) FROM presales.embeddings e WHERE e.build_id=b.id) AS actual_vectors
            FROM presales.index_builds b WHERE b.id=:id AND b.version_id=:version""",
            id=build_id,
            version=version_id,
        )
        if not builds:
            raise NotReady()
        build = builds[0]
        if (
            build["state"] != "ready"
            or not 0 < build["chunk_count"] <= MAX_CHUNKS
            or build["chunk_count"] != build["actual_chunks"]
            or build["actual_vectors"] != build["chunk_count"]
        ):
            raise NotReady()
        return build

    async def submit(
        self,
        actor: Actor,
        version_id: UUID,
        build_id: UUID,
        expected_revision: int,
        expected_catalog: int,
        request_id: UUID,
        *,
        action: KnowledgeAction = "publish",
        replaces: UUID | None = None,
    ) -> dict:
        if action not in {"publish", "replace", "retire"} or (action == "replace") != (
            replaces is not None
        ):
            raise InvalidInput()
        fingerprint = digest(
            [version_id, build_id, expected_revision, expected_catalog, action, replaces]
        )
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            version = await self.registry.version(conn, policy, version_id)
            previous = await self.registry._receipt(
                conn, actor, "submit_knowledge", request_id, fingerprint
            )
            if previous:
                if version["revision"] != previous["revision"] or version["state"] != (
                    "published" if action == "retire" else "awaiting_review"
                ):
                    raise Conflict()
                return previous
            await lock(conn, CATALOG_LOCK)
            version = await self.registry.version(conn, policy, version_id)
            catalog = (
                await rows(conn, "SELECT revision FROM presales.knowledge_catalog FOR UPDATE")
            )[0]["revision"]
            if version["revision"] != expected_revision or catalog != expected_catalog:
                raise Conflict()
            if version["state"] not in (
                {"published"} if action == "retire" else {"candidate", "awaiting_review"}
            ):
                raise Conflict()
            build = await self._complete_build(conn, version_id, build_id)
            if action == "retire":
                if not await rows(
                    conn,
                    "SELECT 1 FROM presales.knowledge_directory WHERE version_id=:version AND build_id=:build",
                    version=version_id,
                    build=build_id,
                ):
                    raise Conflict()
            else:
                if build["replaces"] != replaces:
                    raise Conflict()
                if replaces:
                    old = await self.registry.version(conn, policy, replaces)
                    if (
                        old["document_id"] != version["document_id"]
                        or old["state"] != "published"
                        or not scope_of(old).overlaps(scope_of(version))
                    ):
                        raise Conflict()
            review_id = uuid4()
            revision = expected_revision + 1
            await execute(
                conn,
                """INSERT INTO presales.knowledge_reviews
                (id,version_id,version_revision,catalog_revision,build_id,action,replaces,scope,submitted_by)
                VALUES(:id,:version,:revision,:catalog,:build,:action,:replaces,CAST(:scope AS jsonb),:actor)""",
                id=review_id,
                version=version_id,
                revision=revision,
                catalog=catalog,
                build=build_id,
                action=action,
                replaces=replaces,
                scope=scope_of(version).model_dump_json(),
                actor=actor.subject,
            )
            await execute(
                conn,
                "UPDATE presales.knowledge_versions SET revision=:revision,state=:state WHERE id=:id",
                id=version_id,
                revision=revision,
                state="published" if action == "retire" else "awaiting_review",
            )
            result = {
                "review_id": str(review_id),
                "version_id": str(version_id),
                "revision": revision,
            }
            await self.registry._save_receipt(
                conn, actor, "submit_knowledge", request_id, fingerprint, result
            )
            await audit(
                conn,
                str(actor.subject),
                "submit_knowledge",
                version_id,
                "success",
                policy.authorization_revision,
                request_id,
                revision,
            )
            return result

    async def adjust_candidate_scope(
        self,
        actor: Actor,
        version_id: UUID,
        scope: Applicability,
        expected_revision: int,
        expected_catalog: int,
        reason: str,
        request_id: UUID,
    ) -> dict:
        if not reason.strip() or len(reason) > 1000:
            raise InvalidInput()
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            if policy.role != Role.REVIEWER:
                raise AccessDenied()
            version = await self.registry.version(conn, policy, version_id)
            fingerprint = digest(
                [version_id, scope.model_dump(), expected_revision, expected_catalog, reason]
            )
            if version["submitted_by"] == actor.subject:
                raise AccessDenied()
            previous = await self.registry._receipt(
                conn, actor, "adjust_knowledge_scope", request_id, fingerprint
            )
            if previous:
                if previous["revision"] != version["revision"] or version["state"] != "candidate":
                    raise Conflict()
                return previous
            await lock(conn, CATALOG_LOCK)
            version = await self.registry.version(conn, policy, version_id)
            catalog = (
                await rows(conn, "SELECT revision FROM presales.knowledge_catalog FOR UPDATE")
            )[0]["revision"]
            if (
                version["state"] not in {"candidate", "awaiting_review"}
                or version["revision"] != expected_revision
                or catalog != expected_catalog
            ):
                raise Conflict()
            before = scope_of(version)
            if before == scope:
                raise InvalidInput()
            await execute(
                conn,
                """UPDATE presales.knowledge_versions SET product_scope=CAST(:products AS jsonb),
                deployment_scope=CAST(:deployments AS jsonb),state='candidate',revision=revision+1 WHERE id=:id""",
                id=version_id,
                products=canonical(scope.products),
                deployments=canonical(scope.deployments),
            )
            await execute(conn, "UPDATE presales.knowledge_catalog SET revision=revision+1")
            await execute(
                conn,
                """INSERT INTO presales.knowledge_changes
                (id,version_id,change_type,before_scope,after_scope,catalog_revision,actor,reason)
                VALUES(:id,:version,'scope_adjusted',CAST(:before AS jsonb),CAST(:after AS jsonb),:catalog,:actor,:reason)""",
                id=uuid4(),
                version=version_id,
                before=before.model_dump_json(),
                after=scope.model_dump_json(),
                catalog=catalog + 1,
                actor=actor.subject,
                reason=reason,
            )
            result = {
                "version_id": str(version_id),
                "revision": expected_revision + 1,
                "catalog_revision": catalog + 1,
            }
            await self.registry._save_receipt(
                conn, actor, "adjust_knowledge_scope", request_id, fingerprint, result
            )
            await audit(
                conn,
                str(actor.subject),
                "adjust_knowledge_scope",
                version_id,
                "success",
                policy.authorization_revision,
                request_id,
                expected_revision + 1,
            )
            return result

    async def decide(
        self, actor: Actor, review_id: UUID, approved: bool, reason: str, request_id: UUID
    ) -> dict:
        if not isinstance(approved, bool) or not reason.strip() or len(reason) > 1000:
            raise InvalidInput()
        fingerprint = digest([review_id, approved, reason])
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            if policy.role != Role.REVIEWER:
                raise AccessDenied()
            reviews = await rows(
                conn, "SELECT * FROM presales.knowledge_reviews WHERE id=:id", id=review_id
            )
            if not reviews:
                raise AccessDenied()
            review = reviews[0]
            version = await self.registry.version(conn, policy, review["version_id"])
            if actor.subject == review["submitted_by"] or actor.subject == version["submitted_by"]:
                raise AccessDenied()
            if review["replaces"]:
                await self.registry.version(conn, policy, review["replaces"])
            previous = await self.registry._receipt(
                conn, actor, "decide_knowledge", request_id, fingerprint
            )
            if previous:
                current = (
                    await rows(
                        conn,
                        "SELECT revision,state FROM presales.knowledge_versions WHERE id=:id",
                        id=review["version_id"],
                    )
                )[0]
                if (
                    current["revision"] != previous["revision"]
                    or current["state"] != previous["state"]
                ):
                    raise Conflict()
                return previous
            await lock(conn, CATALOG_LOCK)
            catalog = (
                await rows(conn, "SELECT revision FROM presales.knowledge_catalog FOR UPDATE")
            )[0]["revision"]
            version = await self.registry.version(conn, policy, review["version_id"])
            if (
                review["catalog_revision"] != catalog
                or review["version_revision"] != version["revision"]
                or review["scope"] != scope_of(version).model_dump(mode="json")
            ):
                raise Conflict()
            if await rows(
                conn, "SELECT 1 FROM presales.knowledge_decisions WHERE review_id=:id", id=review_id
            ):
                raise Conflict()
            action = review["action"]
            before_scope = {}
            if version["state"] != ("published" if action == "retire" else "awaiting_review"):
                raise Conflict()
            if approved:
                build = await self._complete_build(conn, version["id"], review["build_id"])
                if action == "retire":
                    before_scope = scope_of(version).model_dump()
                    if not await rows(
                        conn,
                        "SELECT 1 FROM presales.knowledge_directory WHERE version_id=:id AND build_id=:build",
                        id=version["id"],
                        build=build["id"],
                    ):
                        raise Conflict()
                    await execute(
                        conn,
                        "DELETE FROM presales.knowledge_directory WHERE version_id=:id",
                        id=version["id"],
                    )
                    state = "retired"
                else:
                    count = (
                        await rows(
                            conn, "SELECT count(*) AS total FROM presales.knowledge_versions"
                        )
                    )[0]["total"]
                    if count > MAX_VERSIONS:
                        raise InvalidInput()
                    others = await rows(
                        conn,
                        "SELECT * FROM presales.knowledge_versions WHERE document_id=:document AND id<>:id AND state IN ('candidate','awaiting_review','published')",
                        document=version["document_id"],
                        id=version["id"],
                    )
                    conflicts = [v for v in others if scope_of(v).overlaps(scope_of(version))]
                    expected_conflicts = {review["replaces"]} if action == "replace" else set()
                    if {v["id"] for v in conflicts} != expected_conflicts:
                        raise Conflict()
                    if action == "replace":
                        old = await self.registry.version(conn, policy, review["replaces"])
                        before_scope = scope_of(old).model_dump()
                        if old["state"] != "published" or not await rows(
                            conn,
                            "SELECT 1 FROM presales.knowledge_directory WHERE version_id=:id",
                            id=old["id"],
                        ):
                            raise Conflict()
                    total = (
                        await rows(
                            conn,
                            """SELECT coalesce(sum(b.chunk_count),0) AS total
                        FROM presales.knowledge_directory d JOIN presales.index_builds b ON b.id=d.build_id
                        WHERE d.version_id<>:excluded""",
                            excluded=review["replaces"] or version["id"],
                        )
                    )[0]["total"]
                    if total + build["chunk_count"] > MAX_CHUNKS:
                        raise InvalidInput()
                    if action == "replace":
                        await execute(
                            conn,
                            "DELETE FROM presales.knowledge_directory WHERE version_id=:id",
                            id=review["replaces"],
                        )
                        await execute(
                            conn,
                            "UPDATE presales.knowledge_versions SET state='replaced',revision=revision+1 WHERE id=:id",
                            id=review["replaces"],
                        )
                    await execute(
                        conn,
                        "INSERT INTO presales.knowledge_directory VALUES(:version,:build)",
                        version=version["id"],
                        build=build["id"],
                    )
                    state = "published"
                catalog += 1
                await execute(
                    conn,
                    "UPDATE presales.knowledge_catalog SET revision=:revision",
                    revision=catalog,
                )
                await execute(
                    conn,
                    """INSERT INTO presales.knowledge_changes
                    (id,version_id,change_type,before_scope,after_scope,catalog_revision,actor,reason)
                    VALUES(:id,:version,:action,CAST(:before AS jsonb),CAST(:after AS jsonb),:revision,:actor,:reason)""",
                    id=uuid4(),
                    version=version["id"],
                    action=action,
                    before=canonical(before_scope),
                    after=canonical({} if action == "retire" else scope_of(version).model_dump()),
                    revision=catalog,
                    actor=actor.subject,
                    reason=reason,
                )
            else:
                state = "published" if action == "retire" else "candidate"
            decision_id = uuid4()
            await execute(
                conn,
                "INSERT INTO presales.knowledge_decisions(id,review_id,reviewer,approved,reason) VALUES(:id,:review,:actor,:approved,:reason)",
                id=decision_id,
                review=review_id,
                actor=actor.subject,
                approved=approved,
                reason=reason,
            )
            revision = version["revision"] + 1
            await execute(
                conn,
                "UPDATE presales.knowledge_versions SET state=:state,revision=:revision WHERE id=:id",
                id=version["id"],
                state=state,
                revision=revision,
            )
            result = {
                "decision_id": str(decision_id),
                "state": state,
                "revision": revision,
                "catalog_revision": catalog,
            }
            await self.registry._save_receipt(
                conn, actor, "decide_knowledge", request_id, fingerprint, result
            )
            await audit(
                conn,
                str(actor.subject),
                "decide_knowledge",
                version["id"],
                state,
                policy.authorization_revision,
                request_id,
                revision,
            )
            return result
