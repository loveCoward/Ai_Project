import hashlib
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from presales.access.policy import Policy
from presales.domain.contracts import (
    AccessDenied,
    Actor,
    Conflict,
    DomainError,
    DraftContent,
    InvalidInput,
    NotReady,
    SourceDeclaration,
    canonical,
    digest,
)
from presales.infrastructure.database import GRAPH_LOCK, Database, execute, lock, rows
from presales.infrastructure.files import LocalFiles

"""售前业务的服务层，向上层提供了创建任务、注册材料、创建草稿等功能，处理业务逻辑和数据存储。
"""


# 公共审计函数，用于记录操作日志，包括操作人、操作类型、目标对象、结果等信息，确保系统的可追溯性和安全性。
async def audit(
    conn: AsyncConnection,
    actor: str,
    operation: str,
    target: UUID | None,
    result: str,
    authorization_revision: int,
    request_id: UUID | None = None,
    revision: int | None = None,
) -> None:
    await execute(
        conn,
        """INSERT INTO audit.events
        (id,actor,operation,target,revision,result,request_id,authorization_revision)
        VALUES(:id,:actor,:op,:target,:revision,:result,:request,:auth)""",
        id=uuid4(),
        actor=actor,
        op=operation,
        target=target,
        revision=revision,
        result=result,
        request=request_id,
        auth=authorization_revision,
    )


# 售前业务服务类，提供了创建任务、注册材料、创建草稿等功能，处理业务逻辑和数据存储。
class PresalesService:
    def __init__(self, database: Database, files: LocalFiles) -> None:
        self.database = database
        self.files = files

    # 处理拒绝访问的情况，记录审计日志并抛出相应的领域错误。
    async def _denied(
        self, actor: Actor, op: str, target: UUID, request: UUID | None, error: DomainError
    ) -> None:
        async with self.database.transaction() as conn:
            revision = (await rows(conn, "SELECT revision FROM iam.authorization_revision"))[0][
                "revision"
            ]
            await audit(conn, str(actor.subject), op, target, error.code, revision, request)

    async def _result_access(self, policy: Policy, result: dict) -> None:
        await policy.resource(UUID(result["resource_id"]))
        if result.get("source_version_id"):
            await policy.source_closure(UUID(result["source_version_id"]))

    # 执行命令的通用方法，处理事务、锁定、审计和结果访问控制，确保操作的原子性和一致性。
    async def _command(
        self,
        actor: Actor,
        operation: str,
        target: UUID,
        request_id: UUID,
        parameters: dict,
        work: Callable[[AsyncConnection, Policy], Awaitable[dict]],
    ) -> dict:
        try:
            async with self.database.transaction() as conn:
                policy = Policy(conn, actor)
                await policy.start(architect=True)
                await policy.resource(target)
                key = int(digest([str(actor.subject), operation, str(request_id)])[:7], 16) + 100
                await lock(conn, key)
                receipts = await rows(
                    conn,
                    """SELECT parameter_digest,result FROM presales.command_receipts
                    WHERE actor=:actor AND operation=:op AND request_id=:request""",
                    actor=actor.subject,
                    op=operation,
                    request=request_id,
                )
                fingerprint = digest({"target": target, "parameters": parameters})
                if receipts:
                    if receipts[0]["parameter_digest"] != fingerprint:
                        raise Conflict()
                    await self._result_access(policy, receipts[0]["result"])
                    return receipts[0]["result"]
                result = await work(conn, policy)
                await execute(
                    conn,
                    """INSERT INTO presales.command_receipts
                    (actor,operation,request_id,parameter_digest,result)
                    VALUES(:actor,:op,:request,:digest,CAST(:result AS jsonb))""",
                    actor=actor.subject,
                    op=operation,
                    request=request_id,
                    digest=fingerprint,
                    result=canonical(result),
                )
                await audit(
                    conn,
                    str(actor.subject),
                    operation,
                    target,
                    "success",
                    policy.authorization_revision,
                    request_id,
                    result.get("revision"),
                )
                return result
        except DomainError as error:
            await self._denied(actor, operation, target, request_id, error)
            raise

    # 从有效商机创建售前任务
    async def create_task(
        self, actor: Actor, opportunity_id: UUID, goal: str, request_id: UUID
    ) -> dict:
        if not goal.strip() or len(goal) > 5000:
            await self._denied(actor, "create_task", opportunity_id, request_id, InvalidInput())
            raise InvalidInput()

        async def work(conn: AsyncConnection, policy: Policy) -> dict:
            opportunity = await rows(
                conn, "SELECT state FROM presales.opportunities WHERE id=:id", id=opportunity_id
            )
            if not opportunity or opportunity[0]["state"] != "active":
                raise AccessDenied()
            task_id, source_id = uuid4(), uuid4()
            await execute(
                conn,
                "INSERT INTO presales.resources VALUES(:id,'task',:parent)",
                id=task_id,
                parent=opportunity_id,
            )
            await execute(
                conn,
                """INSERT INTO presales.tasks(id,opportunity_id,goal,created_by)
                VALUES(:id,:opportunity,:goal,:actor)""",
                id=task_id,
                opportunity=opportunity_id,
                goal=goal,
                actor=actor.subject,
            )
            await execute(
                conn,
                """INSERT INTO presales.resource_versions
                (id,resource_id,version_no,source_kind,declaration,ready,dependencies_complete,created_by)
                VALUES(:id,:resource,1,'original','owned',true,true,:actor)""",
                id=source_id,
                resource=task_id,
                actor=actor.subject,
            )
            return {"resource_id": str(task_id), "revision": 1, "input_revision": 1}

        return await self._command(
            actor, "create_task", opportunity_id, request_id, {"goal": goal}, work
        )

    async def _task(self, conn: AsyncConnection, task_id: UUID, expected_revision: int) -> dict:
        values = await rows(
            conn, "SELECT * FROM presales.tasks WHERE id=:id FOR UPDATE", id=task_id
        )
        if not values:
            raise AccessDenied()
        task = values[0]
        if task["revision"] != expected_revision or task["state"] != "new":
            raise Conflict()
        return task

    async def read_task(self, actor: Actor, task_id: UUID) -> dict:
        try:
            return await self._read_task(actor, task_id)
        except DomainError as error:
            await self._denied(actor, "read_task", task_id, None, error)
            raise

    async def _read_task(self, actor: Actor, task_id: UUID) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            await policy.resource(task_id)
            task = await rows(conn, "SELECT * FROM presales.tasks WHERE id=:id", id=task_id)
            if not task:
                raise AccessDenied()
            await audit(
                conn,
                str(actor.subject),
                "read_task",
                task_id,
                "success",
                policy.authorization_revision,
            )
            return task[0]

    # 登记客户材料
    async def register_material(
        self,
        actor: Actor,
        parent_id: UUID,
        filename: str,
        data: bytes,
        declaration: SourceDeclaration,
        request_id: UUID,
        expected_task_revision: int | None = None,
    ) -> dict:
        if declaration == SourceDeclaration.SENSITIVE or not filename or len(filename) > 500:
            await self._denied(actor, "register_material", parent_id, request_id, InvalidInput())
            raise InvalidInput()
        if len(data) > 20 * 1024 * 1024:
            await self._denied(actor, "register_material", parent_id, request_id, InvalidInput())
            raise InvalidInput()
        checksum = hashlib.sha256(data).hexdigest()

        async def work(conn: AsyncConnection, policy: Policy) -> dict:
            parent = await policy.resource(parent_id)
            if parent["kind"] not in {"opportunity", "task"}:
                raise InvalidInput()
            task = None
            if parent["kind"] == "task":
                if expected_task_revision is None:
                    raise InvalidInput()
                task = await self._task(conn, parent_id, expected_task_revision)
                counts = (
                    await rows(
                        conn,
                        """SELECT count(*) AS files,coalesce(sum(m.size_bytes),0) AS bytes
                    FROM presales.task_inputs i JOIN presales.material_versions m
                    ON i.material_version_id=m.id WHERE i.task_id=:id""",
                        id=parent_id,
                    )
                )[0]
                if counts["files"] >= 10 or counts["bytes"] + len(data) > 50 * 1024 * 1024:
                    raise InvalidInput()
            material_id, version_id = uuid4(), uuid4()
            key = f"{version_id.hex}.bin"
            await execute(
                conn,
                "INSERT INTO presales.resources VALUES(:id,'material',:parent)",
                id=material_id,
                parent=parent_id,
            )
            await execute(
                conn,
                """INSERT INTO presales.materials(id,opportunity_id,task_id,filename)
                VALUES(:id,:opportunity,:task,:filename)""",
                id=material_id,
                filename=filename,
                opportunity=parent_id if task is None else None,
                task=parent_id if task else None,
            )
            await execute(
                conn,
                """INSERT INTO presales.resource_versions
                (id,resource_id,version_no,source_kind,declaration,created_by)
                VALUES(:id,:resource,1,'original',:declaration,:actor)""",
                id=version_id,
                resource=material_id,
                declaration=declaration.value,
                actor=actor.subject,
            )
            await execute(
                conn,
                """INSERT INTO presales.material_versions
                VALUES(:id,:material,:key,:digest,:size)""",
                id=version_id,
                material=material_id,
                key=key,
                digest=checksum,
                size=len(data),
            )
            await execute(
                conn,
                "INSERT INTO presales.file_readiness(id,state) VALUES(:id,'pending')",
                id=version_id,
            )
            result = {
                "resource_id": str(material_id),
                "material_version_id": str(version_id),
                "storage_key": key,
            }
            if task:
                await execute(
                    conn,
                    "INSERT INTO presales.task_inputs VALUES(:task,:version)",
                    task=parent_id,
                    version=version_id,
                )
                await execute(
                    conn,
                    """UPDATE presales.tasks SET revision=revision+1,
                    input_revision=input_revision+1,updated_at=now() WHERE id=:id""",
                    id=parent_id,
                )
                result["revision"] = task["revision"] + 1
            return result

        result = await self._command(
            actor,
            "register_material",
            parent_id,
            request_id,
            {
                "filename": filename,
                "sha256": checksum,
                "size": len(data),
                "declaration": declaration,
                "expected_revision": expected_task_revision,
            },
            work,
        )
        metadata = await self._material_metadata(
            actor, UUID(result["material_version_id"]), allow_pending=True
        )
        self.files.write(metadata["storage_key"], data, checksum)
        if declaration != SourceDeclaration.UNKNOWN:
            await self.reconcile_material(actor, metadata["id"])
        return result

    # 获取材料的元数据，包括存储键、SHA256校验和、大小等信息，并进行访问控制和状态检查。
    async def _material_metadata(
        self, actor: Actor, version_id: UUID, *, allow_pending: bool = False
    ) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=allow_pending)
            values = await rows(
                conn,
                """SELECT m.*,v.resource_id,v.ready,v.declaration
                FROM presales.material_versions m JOIN presales.resource_versions v ON m.id=v.id
                WHERE m.id=:id""",
                id=version_id,
            )
            if not values:
                raise AccessDenied()
            await policy.resource(values[0]["resource_id"])
            if not allow_pending:
                await policy.source_closure(version_id)
            return values[0]

    # 外部入口：调用内部实现方法，并处理异常，记录审计日志。
    async def reconcile_material(self, actor: Actor, version_id: UUID) -> None:
        try:
            await self._reconcile_material(actor, version_id)
        except DomainError as error:
            await self._denied(actor, "reconcile_material", version_id, None, error)
            raise

    # 内部实现：执行具体业务逻辑，包括读取材料文件、校验文件是否匹配记录、更新数据库状态等操作。
    async def _reconcile_material(self, actor: Actor, version_id: UUID) -> None:
        metadata = await self._material_metadata(actor, version_id, allow_pending=True)
        if metadata["declaration"] == SourceDeclaration.UNKNOWN:
            raise NotReady()
        failure = None
        try:
            self.files.read(metadata["storage_key"], metadata["sha256"], metadata["size_bytes"])
        except (NotReady, InvalidInput) as error:
            failure = error
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            await policy.resource(metadata["resource_id"])
            await execute(
                conn,
                """UPDATE presales.file_readiness SET state=:state,last_error=:error,
                updated_at=now() WHERE id=:id""",
                id=version_id,
                state="failed" if failure else "ready",
                error="file_mismatch" if failure else None,
            )
            if not failure:
                await execute(
                    conn,
                    """UPDATE presales.resource_versions SET ready=true,dependencies_complete=true
                    WHERE id=:id AND ready=false""",
                    id=version_id,
                )
            await audit(
                conn,
                str(actor.subject),
                "reconcile_material",
                version_id,
                "file_mismatch" if failure else "success",
                policy.authorization_revision,
            )
        if failure:
            raise failure

    # 外部入口：读取材料文件内容，调用内部实现方法，并处理异常，记录审计日志。
    async def read_material(self, actor: Actor, version_id: UUID) -> bytes:
        try:
            return await self._read_material(actor, version_id)
        except DomainError as error:
            await self._denied(actor, "read_material", version_id, None, error)
            raise

    # 内部实现：读取材料文件内容，包括访问控制和文件读取操作。
    async def _read_material(self, actor: Actor, version_id: UUID) -> bytes:
        metadata = await self._material_metadata(actor, version_id)
        try:
            data = self.files.read(
                metadata["storage_key"], metadata["sha256"], metadata["size_bytes"]
            )
        except (NotReady, InvalidInput):
            async with self.database.transaction() as conn:
                policy = Policy(conn, actor)
                await policy.start()
                await policy.resource(metadata["resource_id"])
                await execute(
                    conn,
                    """UPDATE presales.file_readiness SET state='failed',
                    last_error='file_mismatch',updated_at=now() WHERE id=:id""",
                    id=version_id,
                )
                await audit(
                    conn,
                    str(actor.subject),
                    "file_integrity_failed",
                    version_id,
                    "file_mismatch",
                    policy.authorization_revision,
                )
            raise
        await self._material_metadata(actor, version_id)
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            await policy.source_closure(version_id)
            await audit(
                conn,
                str(actor.subject),
                "read_material",
                version_id,
                "success",
                policy.authorization_revision,
            )
        return data

    # 获取任务的输入材料的版本ID列表，包括直接输入和间接依赖的材料，进行访问控制和完整性检查。
    async def _inputs(
        self,
        conn: AsyncConnection,
        policy: Policy,
        task_id: UUID,
        previous: UUID | None,
        evidence: list[UUID],
    ) -> list[UUID]:
        inputs = await rows(
            conn,
            "SELECT material_version_id AS id FROM presales.task_inputs WHERE task_id=:id",
            id=task_id,
        )
        roots = await rows(
            conn, "SELECT id FROM presales.resource_versions WHERE resource_id=:id", id=task_id
        )
        source_ids = {item["id"] for item in inputs + roots} | set(evidence)
        if previous:
            source_ids.add(previous)
        if not source_ids:
            raise NotReady()
        input_material_ids = {item["id"] for item in inputs}
        complete_sources: set[UUID] = set()
        for source_id in source_ids:
            source = await rows(
                conn,
                """SELECT v.resource_id,r.kind FROM presales.resource_versions v
                   JOIN presales.resources r ON r.id=v.resource_id WHERE v.id=:id""",
                id=source_id,
            )
            if not source:
                raise AccessDenied()
            source_resource = source[0]
            await policy.resource(source_resource["resource_id"])
            if source_id in evidence and source_resource["kind"] not in {"material", "knowledge"}:
                raise InvalidInput()
            closure = await policy.source_closure(source_id)
            complete_sources.update(closure)
            for inherited_id in closure:
                inherited = await rows(
                    conn,
                    """SELECT v.resource_id,r.kind FROM presales.resource_versions v
                    JOIN presales.resources r ON r.id=v.resource_id WHERE v.id=:id""",
                    id=inherited_id,
                )
                if not inherited:
                    raise AccessDenied()
                inherited_resource = await policy.resource(inherited[0]["resource_id"])
                if inherited[0]["kind"] == "task" and inherited_resource["id"] != task_id:
                    raise AccessDenied()
                if inherited[0]["kind"] == "proposal":
                    owner = (
                        await rows(
                            conn,
                            "SELECT task_id FROM presales.proposal_versions WHERE id=:id",
                            id=inherited_resource["id"],
                        )
                    )[0]
                    if owner["task_id"] != task_id:
                        raise AccessDenied()
                if inherited[0]["kind"] == "material":
                    material = (
                        await rows(
                            conn,
                            "SELECT task_id,opportunity_id FROM presales.materials WHERE id=:id",
                            id=inherited_resource["id"],
                        )
                    )[0]
                    task = (
                        await rows(
                            conn,
                            "SELECT opportunity_id FROM presales.tasks WHERE id=:id",
                            id=task_id,
                        )
                    )[0]
                    if material["task_id"] not in {None, task_id} or (
                        material["opportunity_id"] is not None
                        and material["opportunity_id"] != task["opportunity_id"]
                    ):
                        raise AccessDenied()
                    if inherited_id not in input_material_ids and inherited_id not in evidence:
                        raise AccessDenied()
        material_inputs = await rows(
            conn,
            """SELECT DISTINCT m.id,m.size_bytes FROM presales.material_versions m
            WHERE m.id=ANY(:ids)""",
            ids=list(complete_sources),
        )
        if (
            len(material_inputs) > 10
            or sum(item["size_bytes"] for item in material_inputs) > 50 * 1024 * 1024
        ):
            raise InvalidInput()
        return sorted(source_ids, key=str)

    async def _save_revision(
        self,
        conn: AsyncConnection,
        actor: Actor,
        task: dict,
        version: dict,
        content: DraftContent,
        source_ids: list[UUID],
    ) -> dict:
        await lock(conn, GRAPH_LOCK)
        revision_id, number = uuid4(), version["revision"] + 1
        body = {
            "schema_version": "1.0",
            "identity": {
                "task_id": str(task["id"]),
                "opportunity_id": str(task["opportunity_id"]),
                "version_id": str(version["id"]),
                "revision": number,
                "analysis_attempt_id": None,
            },
            "provenance": {"mode": "manual_draft", "editor": str(actor.subject)},
            "access": {"direct_source_version_ids": [str(item) for item in source_ids]},
            "draft": content.model_dump(mode="json"),
            "incomplete_sections": [
                "requirements",
                "coverage",
                "solution",
                "risks",
                "clarifications",
                "review",
                "approval",
                "execution",
                "memory_updates",
            ],
        }
        await execute(
            conn,
            """INSERT INTO presales.resource_versions
            (id,resource_id,version_no,source_kind,input_manifest,created_by)
            VALUES(:id,:resource,:number,'derived',CAST(:inputs AS jsonb),:actor)""",
            id=revision_id,
            resource=version["id"],
            number=number,
            inputs=canonical(source_ids),
            actor=actor.subject,
        )
        for source in source_ids:
            await execute(
                conn,
                "INSERT INTO presales.direct_dependencies VALUES(:downstream,:upstream,'actual_input')",
                downstream=revision_id,
                upstream=source,
            )
        await execute(
            conn,
            """INSERT INTO presales.proposal_revisions
            (id,version_id,revision_no,schema_version,body,editor)
            VALUES(:id,:version,:number,'1.0',CAST(:body AS jsonb),:actor)""",
            id=revision_id,
            version=version["id"],
            number=number,
            body=canonical(body),
            actor=actor.subject,
        )
        await execute(
            conn,
            "UPDATE presales.resource_versions SET ready=true,dependencies_complete=true WHERE id=:id",
            id=revision_id,
        )
        await execute(
            conn,
            "UPDATE presales.proposal_versions SET current_revision_id=:revision,revision=:number WHERE id=:id",
            revision=revision_id,
            number=number,
            id=version["id"],
        )
        await execute(
            conn,
            "UPDATE presales.tasks SET candidate_version_id=:version,revision=revision+1,updated_at=now() WHERE id=:id",
            version=version["id"],
            id=task["id"],
        )
        return {
            "resource_id": str(version["id"]),
            "source_version_id": str(revision_id),
            "revision": number,
            "task_revision": task["revision"] + 1,
        }

    async def create_draft(
        self,
        actor: Actor,
        task_id: UUID,
        expected_revision: int,
        request_id: UUID,
        content: DraftContent,
    ) -> dict:
        async def work(conn: AsyncConnection, policy: Policy) -> dict:
            task = await self._task(conn, task_id, expected_revision)
            if task["candidate_version_id"]:
                raise Conflict()
            source_ids = await self._inputs(
                conn, policy, task_id, None, content.evidence_version_ids
            )
            version_id = uuid4()
            await execute(
                conn,
                "INSERT INTO presales.resources VALUES(:id,'proposal',:task)",
                id=version_id,
                task=task_id,
            )
            version = (
                await rows(
                    conn,
                    """INSERT INTO presales.proposal_versions(id,task_id,version_no)
                VALUES(:id,:task,1) RETURNING *""",
                    id=version_id,
                    task=task_id,
                )
            )[0]
            return await self._save_revision(conn, actor, task, version, content, source_ids)

        return await self._command(
            actor,
            "create_draft",
            task_id,
            request_id,
            {"expected_revision": expected_revision, "content": content.model_dump(mode="json")},
            work,
        )

    # 保存草稿并记录来源依赖，包括对草稿内容进行验证、检查任务和版本的状态、处理输入材料的访问控制，并将草稿保存为新的修订版本。
    async def save_draft(
        self,
        actor: Actor,
        version_id: UUID,
        expected_task_revision: int,
        expected_revision: int,
        request_id: UUID,
        content: DraftContent,
    ) -> dict:
        async def work(conn: AsyncConnection, policy: Policy) -> dict:
            values = await rows(
                conn, "SELECT * FROM presales.proposal_versions WHERE id=:id", id=version_id
            )
            if not values:
                raise AccessDenied()
            task = await self._task(conn, values[0]["task_id"], expected_task_revision)
            version = (
                await rows(
                    conn,
                    "SELECT * FROM presales.proposal_versions WHERE id=:id FOR UPDATE",
                    id=version_id,
                )
            )[0]
            if version["revision"] != expected_revision or version["state"] not in {
                "draft",
                "changes_requested",
            }:
                raise Conflict()
            if task["candidate_version_id"] != version_id:
                raise Conflict()
            source_ids = await self._inputs(
                conn,
                policy,
                task["id"],
                version["current_revision_id"],
                content.evidence_version_ids,
            )
            return await self._save_revision(conn, actor, task, version, content, source_ids)

        return await self._command(
            actor,
            "save_draft",
            version_id,
            request_id,
            {
                "expected_task_revision": expected_task_revision,
                "expected_revision": expected_revision,
                "content": content.model_dump(mode="json"),
            },
            work,
        )

    # 读取草稿内容
    async def read_draft(self, actor: Actor, revision_id: UUID) -> dict:
        try:
            async with self.database.transaction() as conn:
                policy = Policy(conn, actor)
                await policy.start()
                await policy.source_closure(revision_id)
                values = await rows(
                    conn,
                    "SELECT body FROM presales.proposal_revisions WHERE id=:id",
                    id=revision_id,
                )
                if not values:
                    raise AccessDenied()
                await audit(
                    conn,
                    str(actor.subject),
                    "read_draft",
                    revision_id,
                    "success",
                    policy.authorization_revision,
                )
                return values[0]["body"]
        except DomainError as error:
            await self._denied(actor, "read_draft", revision_id, None, error)
            raise
