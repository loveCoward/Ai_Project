import asyncio
from uuid import UUID, uuid4

from presales.access.policy import Policy
from presales.application.service import PresalesService, audit
from presales.domain.contracts import (
    AccessDenied,
    Actor,
    Conflict,
    InvalidInput,
    NotReady,
    canonical,
)
from presales.infrastructure.database import GRAPH_LOCK, execute, lock, rows
from presales.knowledge.models import CATALOG_LOCK
from presales.materials.models import (
    MAX_TASK_CHARS,
    PARSER_VERSION,
    ManifestItem,
    ParsedDocument,
    ParseFailure,
)
from presales.materials.parser import ControlledParser


class MaterialService:
    def __init__(self, service: PresalesService, parser: ControlledParser | None = None) -> None:
        self.service = service
        self.database = service.database
        self.parser = parser or ControlledParser()

    async def _source(self, conn, policy: Policy, version_id: UUID) -> dict:
        await policy.source_closure(version_id)
        values = await rows(
            conn,
            """
            SELECT v.resource_id,m.filename,mv.storage_key,mv.sha256,mv.size_bytes,true AS is_material
            FROM presales.material_versions mv JOIN presales.materials m ON m.id=mv.material_id
            JOIN presales.resource_versions v ON v.id=mv.id WHERE mv.id=:id
            UNION ALL
            SELECT k.document_id AS resource_id,k.filename,k.storage_key,k.sha256,k.size_bytes,false AS is_material
            FROM presales.knowledge_versions k WHERE k.id=:id AND k.ready
            """,
            id=version_id,
        )
        if not values:
            raise NotReady()
        source = values[0]
        self.service.files.read(source["storage_key"], source["sha256"], source["size_bytes"])
        return values[0]

    async def parse_material(self, actor: Actor, version_id: UUID) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            source = await self._source(conn, policy, version_id)
            await lock(conn, GRAPH_LOCK)
            jobs = await rows(
                conn,
                """SELECT *,lease_until>now() AS leased FROM presales.parse_jobs
                WHERE source_version_id=:id AND sha256=:sha AND parser_version=:parser FOR UPDATE
                """,
                id=version_id,
                sha=source["sha256"],
                parser=PARSER_VERSION,
            )
            if jobs and jobs[0]["state"] == "succeeded":
                return await self._result(conn, policy, jobs[0]["id"])
            if jobs and jobs[0]["state"] == "running" and jobs[0]["leased"]:
                raise Conflict()
            job_id = jobs[0]["id"] if jobs else uuid4()
            attempt = jobs[0]["attempt"] + 1 if jobs else 1
            if jobs:
                await execute(
                    conn,
                    """UPDATE presales.parse_jobs SET state='running',attempt=:attempt,
                    error_code=NULL,lease_until=now()+interval '65 seconds' WHERE id=:id""",
                    id=job_id,
                    attempt=attempt,
                )
            else:
                await execute(
                    conn,
                    """INSERT INTO presales.parse_jobs
                    (id,source_version_id,sha256,parser_version,state,lease_until,created_by)
                    VALUES(:id,:source,:sha,:parser,'running',now()+interval '65 seconds',:actor)
                    """,
                    id=job_id,
                    source=version_id,
                    sha=source["sha256"],
                    parser=PARSER_VERSION,
                    actor=actor.subject,
                )
        error = None
        document = None
        try:
            if source["resource_id"]:
                data = (
                    await self.service.read_material(actor, version_id)
                    if source["is_material"]
                    else self.service.files.read(
                        source["storage_key"], source["sha256"], source["size_bytes"]
                    )
                )
                document = await asyncio.to_thread(self.parser.parse, source["filename"], data)
        except ParseFailure as failure:
            error = failure.code
        except (NotReady, InvalidInput):
            error = "source_file_not_ready"
        except Exception:
            error = "parser_failed"
        try:
            return await self._finalize(actor, version_id, source, job_id, attempt, document, error)
        except (AccessDenied, NotReady):
            async with self.database.transaction() as conn:
                await execute(
                    conn,
                    """UPDATE presales.parse_jobs SET state='failed',error_code='source_access_changed'
                    WHERE id=:id AND attempt=:attempt AND state='running'""",
                    id=job_id,
                    attempt=attempt,
                )
                await execute(
                    conn,
                    """INSERT INTO presales.parse_errors(id,job_id,attempt,code)
                    SELECT :error,:id,:attempt,'source_access_changed' WHERE EXISTS(
                    SELECT 1 FROM presales.parse_jobs WHERE id=:id AND attempt=:attempt AND state='failed')""",
                    error=uuid4(),
                    id=job_id,
                    attempt=attempt,
                )
            raise

    async def _finalize(
        self,
        actor: Actor,
        version_id: UUID,
        source: dict,
        job_id: UUID,
        attempt: int,
        document: ParsedDocument | None,
        error: str | None,
    ) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            await self._source(conn, policy, version_id)
            await lock(conn, CATALOG_LOCK)
            await lock(conn, GRAPH_LOCK)
            job = (
                await rows(
                    conn, "SELECT * FROM presales.parse_jobs WHERE id=:id FOR UPDATE", id=job_id
                )
            )[0]
            if job["attempt"] != attempt or job["state"] != "running":
                raise Conflict()
            if document is not None and error is None:
                tasks = await rows(
                    conn,
                    "SELECT task_id FROM presales.task_inputs WHERE material_version_id=:id",
                    id=version_id,
                )
                for task in tasks:
                    total = (
                        await rows(
                            conn,
                            """SELECT coalesce(sum(r.character_count),0) AS total
                        FROM presales.task_inputs i JOIN presales.parse_results r
                        ON r.source_version_id=i.material_version_id
                        WHERE i.task_id=:task AND i.material_version_id<>:id""",
                            task=task["task_id"],
                            id=version_id,
                        )
                    )[0]["total"]
                    if total + document.character_count > MAX_TASK_CHARS:
                        error = "task_character_limit"
            if error is None and document is not None:
                result_id = uuid4()
                number = (
                    await rows(
                        conn,
                        "SELECT coalesce(max(version_no),0)+1 AS number FROM presales.resource_versions WHERE resource_id=:id",
                        id=source["resource_id"],
                    )
                )[0]["number"]
                await execute(
                    conn,
                    """INSERT INTO presales.resource_versions
                    (id,resource_id,version_no,source_kind,input_manifest,created_by)
                    VALUES(:id,:resource,:number,'derived',CAST(:manifest AS jsonb),:actor)""",
                    id=result_id,
                    resource=source["resource_id"],
                    number=number,
                    manifest=canonical([version_id]),
                    actor=actor.subject,
                )
                await execute(
                    conn,
                    "INSERT INTO presales.direct_dependencies VALUES(:id,:source,'located_parse')",
                    id=result_id,
                    source=version_id,
                )
                await execute(
                    conn,
                    "UPDATE presales.resource_versions SET ready=true,dependencies_complete=true WHERE id=:id",
                    id=result_id,
                )
                await execute(
                    conn,
                    """INSERT INTO presales.parse_results
                    (id,job_id,source_version_id,document,character_count)
                    VALUES(:id,:job,:source,CAST(:document AS jsonb),:count)""",
                    id=result_id,
                    job=job_id,
                    source=version_id,
                    document=document.model_dump_json(),
                    count=document.character_count,
                )
                await execute(
                    conn,
                    """INSERT INTO presales.source_locations
                    SELECT x.id,:source,x.locator FROM jsonb_to_recordset(CAST(:locations AS jsonb))
                    AS x(id uuid,locator jsonb)""",
                    source=version_id,
                    locations=canonical(
                        [
                            {"id": uuid4(), "locator": unit.location.model_dump()}
                            for unit in document.units
                        ]
                    ),
                )
            else:
                error = error or "parser_failed"
                affected = await rows(
                    conn,
                    """SELECT t.id FROM presales.task_inputs i
                    JOIN presales.tasks t ON t.id=i.task_id
                    LEFT JOIN LATERAL (SELECT necessity FROM presales.task_input_manifest
                      WHERE task_id=t.id AND input_revision<=t.input_revision
                      AND material_version_id=i.material_version_id ORDER BY input_revision DESC LIMIT 1) m ON true
                    WHERE i.material_version_id=:id AND coalesce(m.necessity,'unclassified')<>'optional'""",
                    id=version_id,
                )
                for task in affected:
                    await execute(
                        conn,
                        "SELECT presales.mark_material_supplementation(:actor,:id)",
                        actor=actor.subject,
                        id=task["id"],
                    )
                await execute(
                    conn,
                    "INSERT INTO presales.parse_errors(id,job_id,attempt,code) VALUES(:id,:job,:attempt,:code)",
                    id=uuid4(),
                    job=job_id,
                    attempt=attempt,
                    code=error,
                )
            await execute(
                conn,
                "UPDATE presales.parse_jobs SET state=:state,error_code=:error WHERE id=:id",
                id=job_id,
                state="failed" if error else "succeeded",
                error=error,
            )
            await audit(
                conn,
                str(actor.subject),
                "parse_material",
                version_id,
                error or "success",
                policy.authorization_revision,
            )
            return await self._result(conn, policy, job_id)

    async def _result(self, conn, policy: Policy, job_id: UUID) -> dict:
        job = await rows(conn, "SELECT * FROM presales.parse_jobs WHERE id=:id", id=job_id)
        if not job:
            raise AccessDenied()
        await self._source(conn, policy, job[0]["source_version_id"])
        results = await rows(
            conn, "SELECT * FROM presales.parse_results WHERE job_id=:id", id=job_id
        )
        return {
            "job_id": str(job_id),
            "state": job[0]["state"],
            "error_code": job[0]["error_code"],
            "result_id": str(results[0]["id"]) if results else None,
            "document": results[0]["document"] if results else None,
        }

    async def get_parse_result(self, actor: Actor, job_id: UUID) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            return await self._result(conn, policy, job_id)

    async def set_manifest(
        self, actor: Actor, task_id: UUID, expected_revision: int, items: list[ManifestItem]
    ) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            await policy.resource(task_id)
            task = await rows(
                conn, "SELECT * FROM presales.tasks WHERE id=:id FOR UPDATE", id=task_id
            )
            if (
                not task
                or task[0]["revision"] != expected_revision
                or task[0]["state"] not in {"new", "awaiting_supplementation"}
            ):
                raise Conflict()
            inputs = await rows(
                conn,
                """SELECT i.material_version_id,m.size_bytes FROM presales.task_inputs i
                JOIN presales.material_versions m ON m.id=i.material_version_id WHERE i.task_id=:id""",
                id=task_id,
            )
            ids = {i["material_version_id"] for i in inputs}
            if (
                len(items) != len(ids)
                or {i.material_version_id for i in items} != ids
                or len(ids) > 10
                or sum(i["size_bytes"] for i in inputs) > 50 * 1024 * 1024
            ):
                raise InvalidInput()
            for identifier in ids:
                sources = await rows(
                    conn,
                    "SELECT resource_id FROM presales.resource_versions WHERE id=:id",
                    id=identifier,
                )
                if not sources:
                    raise AccessDenied()
                await policy.resource(sources[0]["resource_id"])
            revision = task[0]["input_revision"] + 1
            for item in items:
                await execute(
                    conn,
                    """INSERT INTO presales.task_input_manifest
                    (task_id,input_revision,material_version_id,necessity,marked_by) VALUES(:task,:revision,:source,:necessity,:actor)""",
                    task=task_id,
                    revision=revision,
                    source=item.material_version_id,
                    necessity=item.necessity,
                    actor=actor.subject,
                )
            await execute(
                conn,
                "UPDATE presales.tasks SET revision=revision+1,input_revision=:revision,updated_at=now() WHERE id=:id",
                id=task_id,
                revision=revision,
            )
            await audit(
                conn,
                str(actor.subject),
                "set_input_manifest",
                task_id,
                "success",
                policy.authorization_revision,
                revision=revision,
            )
        return await self.input_status(actor, task_id)

    async def input_status(self, actor: Actor, task_id: UUID) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            await policy.resource(task_id)
            task = await rows(
                conn, "SELECT * FROM presales.tasks WHERE id=:id FOR UPDATE", id=task_id
            )
            if not task:
                raise AccessDenied()
            inputs = await rows(
                conn,
                """SELECT i.material_version_id,coalesce(m.necessity,'unclassified') AS necessity,
                j.state,j.error_code FROM presales.task_inputs i
                LEFT JOIN LATERAL (SELECT necessity FROM presales.task_input_manifest
                  WHERE task_id=i.task_id AND material_version_id=i.material_version_id
                  AND input_revision<=:revision ORDER BY input_revision DESC LIMIT 1) m ON true
                LEFT JOIN presales.parse_jobs j ON j.source_version_id=i.material_version_id AND j.parser_version=:parser
                WHERE i.task_id=:task""",
                task=task_id,
                revision=task[0]["input_revision"],
                parser=PARSER_VERSION,
            )
            for item in inputs:
                sources = await rows(
                    conn,
                    """SELECT v.resource_id,v.ready,f.state,m.storage_key,m.sha256,m.size_bytes
                    FROM presales.resource_versions v JOIN presales.material_versions m ON m.id=v.id
                    LEFT JOIN presales.file_readiness f ON f.id=v.id WHERE v.id=:id""",
                    id=item["material_version_id"],
                )
                if not sources:
                    raise AccessDenied()
                await policy.resource(sources[0]["resource_id"])
                if not sources[0]["ready"] or sources[0]["state"] != "ready":
                    item["state"] = "not_ready"
                else:
                    try:
                        self.service.files.read(
                            sources[0]["storage_key"],
                            sources[0]["sha256"],
                            sources[0]["size_bytes"],
                        )
                    except (NotReady, InvalidInput):
                        item["state"] = "not_ready"
            unresolved = [
                str(i["material_version_id"])
                for i in inputs
                if i["state"] != "succeeded" and i["necessity"] != "optional"
            ]
            unused = [
                str(i["material_version_id"])
                for i in inputs
                if i["state"] != "succeeded" and i["necessity"] == "optional"
            ]
            if (
                unresolved
                and task[0]["state"] in {"new", "analyzing"}
                and policy.role == "architect"
            ):
                await execute(
                    conn,
                    "SELECT presales.mark_material_supplementation(:actor,:id)",
                    actor=actor.subject,
                    id=task_id,
                )
                await audit(
                    conn,
                    str(actor.subject),
                    "input_supplementation",
                    task_id,
                    "unresolved_inputs",
                    policy.authorization_revision,
                )
            return {
                "complete": bool(inputs) and not unresolved,
                "unresolved": unresolved,
                "unused_sources": unused,
                "input_revision": task[0]["input_revision"],
            }

    async def parsed_document(self, actor: Actor, version_id: UUID) -> tuple[UUID, ParsedDocument]:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            await self._source(conn, policy, version_id)
            values = await rows(
                conn,
                """SELECT r.* FROM presales.parse_results r JOIN presales.parse_jobs j ON j.id=r.job_id
                WHERE r.source_version_id=:id AND j.parser_version=:parser AND j.state='succeeded'""",
                id=version_id,
                parser=PARSER_VERSION,
            )
            if not values:
                raise NotReady()
            return values[0]["id"], ParsedDocument.model_validate(values[0]["document"])
