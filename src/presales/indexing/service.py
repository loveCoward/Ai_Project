import asyncio
import math
import time
from uuid import UUID, uuid4, uuid5

from presales.access.policy import Policy
from presales.application.service import audit
from presales.domain.contracts import (
    AccessDenied,
    Actor,
    Conflict,
    InvalidInput,
    canonical,
    digest,
)
from presales.indexing.chunks import ChunkConfig, split_chunks
from presales.indexing.embedding import Embedder, EmbeddingFailure
from presales.infrastructure.database import execute, lock, rows
from presales.knowledge.models import CATALOG_LOCK, MAX_CHUNKS, MAX_VERSIONS
from presales.knowledge.registry import KnowledgeRegistry, scope_of
from presales.materials.service import MaterialService


class IndexService:
    def __init__(
        self,
        registry: KnowledgeRegistry,
        materials: MaterialService,
        embedding: Embedder,
        *,
        request_seconds: float = 60,
        batch_seconds: float = 600,
        batch_size: int = 32,
    ) -> None:
        if (
            not 0 < request_seconds <= 60
            or not 0 < batch_seconds <= 600
            or not 1 <= batch_size <= 128
        ):
            raise ValueError("Index deadlines or batch size out of bounds")
        self.registry, self.materials, self.embedding = registry, materials, embedding
        self.database = registry.database
        self.request_seconds, self.batch_seconds, self.batch_size = (
            request_seconds,
            batch_seconds,
            batch_size,
        )

    async def _capacity(self, conn, version: dict, count: int, replaces: UUID | None) -> None:
        versions = (await rows(conn, "SELECT count(*) AS total FROM presales.knowledge_versions"))[
            0
        ]["total"]
        if versions > MAX_VERSIONS or count > MAX_CHUNKS or count < 1:
            raise InvalidInput()
        current = await rows(
            conn,
            """SELECT k.*,b.chunk_count FROM presales.knowledge_directory d
            JOIN presales.knowledge_versions k ON k.id=d.version_id JOIN presales.index_builds b ON b.id=d.build_id""",
        )
        if replaces is not None and not any(
            v["id"] == replaces
            and v["document_id"] == version["document_id"]
            and scope_of(v).overlaps(scope_of(version))
            for v in current
        ):
            raise Conflict()
        total = (
            sum(v["chunk_count"] for v in current if v["id"] not in {replaces, version["id"]})
            + count
        )
        if total > MAX_CHUNKS:
            raise InvalidInput()

    async def build(
        self,
        actor: Actor,
        version_id: UUID,
        config: ChunkConfig | None = None,
        *,
        replaces: UUID | None = None,
    ) -> dict:
        config = config or ChunkConfig()
        started = time.monotonic()
        parse_id, document = await self.materials.parsed_document(actor, version_id)
        chunks = split_chunks(document, parse_id, config)
        build_id = uuid4()
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            version = await self.registry.version(conn, policy, version_id)
            if version["state"] != "candidate":
                raise Conflict()
            self.embedding.authorize(version["declaration"])
            await lock(conn, CATALOG_LOCK)
            version = await self.registry.version(conn, policy, version_id)
            if version["state"] != "candidate":
                raise Conflict()
            await self._capacity(conn, version, len(chunks), replaces)
            catalog = (await rows(conn, "SELECT revision FROM presales.knowledge_catalog"))[0][
                "revision"
            ]
            await execute(
                conn,
                """INSERT INTO presales.index_builds
                (id,version_id,parse_result_id,state,config,model_identity,dimensions,mode,chunk_count,replaces,catalog_revision,created_by,deadline)
                VALUES(:id,:version,:parse,'building',CAST(:config AS jsonb),:model,:dimensions,:mode,:count,:replaces,:catalog,:actor,
                    now()+make_interval(secs=>:seconds))""",
                id=build_id,
                version=version_id,
                parse=parse_id,
                config=canonical(
                    {
                        **config.model_dump(),
                        "parser_version": document.parser_version,
                        "request_seconds": self.request_seconds,
                        "batch_seconds": self.batch_seconds,
                        "batch_size": self.batch_size,
                    }
                ),
                model=self.embedding.model_identity,
                dimensions=self.embedding.dimensions,
                mode=self.embedding.mode,
                count=len(chunks),
                replaces=replaces,
                catalog=catalog,
                actor=actor.subject,
                seconds=self.batch_seconds,
            )
            await audit(
                conn,
                str(actor.subject),
                "start_index",
                build_id,
                "building",
                policy.authorization_revision,
            )
        error = None
        vectors: list[list[float]] = []
        identity = (self.embedding.model_identity, self.embedding.dimensions, self.embedding.mode)
        try:
            for offset in range(0, len(chunks), self.batch_size):
                await self._check_active(actor, build_id, version_id)
                batch = chunks[offset : offset + self.batch_size]
                result: list[list[float]] = []
                for attempt in range(3):
                    await self._check_active(actor, build_id, version_id)
                    remaining = self.batch_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        result = await asyncio.wait_for(
                            self.embedding.embed([c.text for c in batch]),
                            timeout=min(self.request_seconds, remaining),
                        )
                        break
                    except (EmbeddingFailure, TimeoutError):
                        if attempt == 2:
                            raise
                if identity != (
                    self.embedding.model_identity,
                    self.embedding.dimensions,
                    self.embedding.mode,
                ) or len(result) != len(batch):
                    raise ValueError("embedding_contract")
                if any(
                    len(v) != identity[1]
                    or any(
                        isinstance(x, bool)
                        or not isinstance(x, (int, float))
                        or not math.isfinite(x)
                        for x in v
                    )
                    for v in result
                ):
                    raise ValueError("embedding_contract")
                vectors.extend(result)
            if time.monotonic() - started >= self.batch_seconds:
                raise TimeoutError
        except asyncio.CancelledError:
            await self._fail(build_id, "cancelled", "cancelled")
            raise
        except AccessDenied:
            await self._fail(build_id, "failed", "authorization_changed")
            raise
        except Conflict:
            await self._fail(build_id, "failed", "index_state_changed")
            return await self.status(actor, build_id)
        except TimeoutError:
            error = "index_timeout"
        except EmbeddingFailure:
            error = "embedding_failed"
        except Exception:
            error = "embedding_contract_failed"
        if error:
            await self._fail(build_id, "failed", error)
            return await self.status(actor, build_id)
        try:
            current = await self.status(actor, build_id)
        except AccessDenied:
            await self._fail(build_id, "failed", "authorization_changed")
            raise
        if current["state"] != "building":
            return current
        try:
            async with self.database.transaction() as conn:
                policy = Policy(conn, actor)
                await policy.start(architect=True)
                version = await self.registry.version(conn, policy, version_id)
                await lock(conn, CATALOG_LOCK)
                version = await self.registry.version(conn, policy, version_id)
                build = (
                    await rows(
                        conn,
                        "SELECT *,deadline>now() AS in_time FROM presales.index_builds WHERE id=:id FOR UPDATE",
                        id=build_id,
                    )
                )[0]
                if (
                    build["state"] != "building"
                    or not build["in_time"]
                    or version["state"] != "candidate"
                ):
                    raise Conflict()
                await self._capacity(conn, version, len(chunks), replaces)
                records = []
                for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                    records.append(
                        {
                            "id": uuid5(build_id, str(chunk.id)),
                            "location_id": uuid4(),
                            "locator": chunk.location.model_dump(),
                            "ordinal": ordinal,
                            "text_content": chunk.text,
                            "sha256": digest(chunk.text),
                            "tokens": chunk.tokens,
                            "start_offset": chunk.start,
                            "end_offset": chunk.end,
                            "embedding": canonical(vector),
                        }
                    )
                payload = canonical(records)
                await execute(
                    conn,
                    """INSERT INTO presales.source_locations
                    SELECT x.location_id,:version,x.locator FROM jsonb_to_recordset(CAST(:data AS jsonb))
                    AS x(location_id uuid,locator jsonb)""",
                    version=version_id,
                    data=payload,
                )
                await execute(
                    conn,
                    """INSERT INTO presales.chunks
                    SELECT x.id,:build,:version,x.location_id,x.ordinal,x.text_content,x.sha256,
                      x.tokens,x.start_offset,x.end_offset FROM jsonb_to_recordset(CAST(:data AS jsonb))
                    AS x(id uuid,location_id uuid,ordinal integer,text_content text,sha256 text,
                         tokens jsonb,start_offset integer,end_offset integer)""",
                    build=build_id,
                    version=version_id,
                    data=payload,
                )
                await execute(
                    conn,
                    """INSERT INTO presales.embeddings
                    SELECT x.id,:build,:model,:dimensions,CAST(x.embedding AS public.vector)
                    FROM jsonb_to_recordset(CAST(:data AS jsonb)) AS x(id uuid,embedding text)""",
                    build=build_id,
                    model=identity[0],
                    dimensions=identity[1],
                    data=payload,
                )
                await execute(
                    conn, "UPDATE presales.index_builds SET state='ready' WHERE id=:id", id=build_id
                )
                await audit(
                    conn,
                    str(actor.subject),
                    "complete_index",
                    build_id,
                    "ready",
                    policy.authorization_revision,
                )
        except AccessDenied:
            await self._fail(build_id, "failed", "authorization_changed")
            raise
        except Exception:
            await self._fail(build_id, "failed", "index_finalization_failed")
            raise
        return await self.status(actor, build_id)

    async def _fail(self, build_id: UUID, state: str, error: str) -> None:
        async with self.database.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.index_builds SET state=:state,error_code=:error WHERE id=:id AND state='building'",
                id=build_id,
                state=state,
                error=error,
            )

    async def _check_active(self, actor: Actor, build_id: UUID, version_id: UUID) -> None:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            version = await self.registry.version(conn, policy, version_id)
            self.embedding.authorize(version["declaration"])
            build = (
                await rows(
                    conn, "SELECT state FROM presales.index_builds WHERE id=:id", id=build_id
                )
            )[0]
            if build["state"] != "building":
                raise Conflict()

    async def status(self, actor: Actor, build_id: UUID) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            builds = await rows(
                conn, "SELECT * FROM presales.index_builds WHERE id=:id", id=build_id
            )
            if not builds:
                raise AccessDenied()
            build = builds[0]
            await self.registry.version(conn, policy, build["version_id"])
            return {
                "build_id": str(build_id),
                "state": build["state"],
                "error_code": build["error_code"],
                "mode": build["mode"],
                "model_identity": build["model_identity"],
                "dimensions": build["dimensions"],
                "chunk_count": build["chunk_count"],
            }

    async def cancel(self, actor: Actor, build_id: UUID) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            builds = await rows(
                conn, "SELECT * FROM presales.index_builds WHERE id=:id FOR UPDATE", id=build_id
            )
            if not builds:
                raise AccessDenied()
            await self.registry.version(conn, policy, builds[0]["version_id"])
            if builds[0]["state"] != "building":
                raise Conflict()
            await execute(
                conn,
                "UPDATE presales.index_builds SET state='cancelled',error_code='cancelled' WHERE id=:id",
                id=build_id,
            )
            await audit(
                conn,
                str(actor.subject),
                "cancel_index",
                build_id,
                "cancelled",
                policy.authorization_revision,
            )
        return await self.status(actor, build_id)
