import asyncio
import math
from collections import OrderedDict
from typing import Protocol
from uuid import UUID, uuid4

import httpx

from presales.access.policy import Policy
from presales.domain.contracts import AccessDenied, Actor, Conflict, NotReady, canonical, digest
from presales.indexing.embedding import Embedder, EmbeddingFailure
from presales.infrastructure.config import OutboundServiceSettings
from presales.infrastructure.database import execute, lock, rows
from presales.infrastructure.outbound import OutboundAdapter, OutboundService
from presales.knowledge.models import CATALOG_LOCK
from presales.knowledge.registry import KnowledgeRegistry, scope_of
from presales.retrieval.models import Evidence, EvidenceQuery, RetrievalConfig, RetrievalResult
from presales.retrieval.ranking import bm25, rrf


class Reranker(Protocol):
    identity: str

    def authorize(self, declaration: str) -> None: ...

    async def rank(self, query: str, documents: list[str]) -> list[int]: ...


class ConfiguredReranker:
    def __init__(self, outbound: OutboundAdapter, settings: OutboundServiceSettings):
        self.outbound, self.settings = outbound, settings
        self.identity = settings.service_identity or ""

    def authorize(self, declaration: str) -> None:
        target = self.outbound.resolve(OutboundService.RERANKER, declaration)
        if target.mode != "real" or target.service_identity != self.identity:
            raise ValueError("Reranker configuration changed")

    async def rank(self, query: str, documents: list[str]) -> list[int]:
        headers = (
            {"Authorization": f"Bearer {self.settings.api_key.get_secret_value()}"}
            if self.settings.api_key
            else {}
        )
        async with httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                str(self.settings.endpoint),
                headers=headers,
                json={
                    "model": self.identity,
                    "query": query,
                    "documents": documents,
                },
            )
            response.raise_for_status()
            payload = response.json()
        if payload.get("model") != self.identity:
            raise ValueError("Reranker identity mismatch")
        return [
            v["index"]
            for v in sorted(payload["results"], key=lambda v: (-v["relevance_score"], v["index"]))
        ]


class RetrievalService:
    def __init__(
        self,
        registry: KnowledgeRegistry,
        embedding: Embedder,
        *,
        config: RetrievalConfig | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.registry, self.database, self.embedding = registry, registry.database, embedding
        self.config = config or RetrievalConfig()
        self.reranker = reranker
        self._cache: OrderedDict[str, dict] = OrderedDict()

    async def _allowed(self, conn, actor: Actor, query: EvidenceQuery):
        policy = Policy(conn, actor)
        await policy.start()
        await lock(conn, CATALOG_LOCK, shared=True)
        catalog = (await rows(conn, "SELECT revision FROM presales.knowledge_catalog"))[0][
            "revision"
        ]
        versions = await rows(
            conn,
            """SELECT k.*,d.build_id,b.model_identity,b.dimensions,b.mode,b.state AS build_state
            FROM presales.knowledge_directory d
            JOIN presales.knowledge_versions k ON k.id=d.version_id
            JOIN presales.index_builds b ON b.id=d.build_id WHERE k.state='published'""",
        )
        allowed = []
        for version in versions:
            if not query.scope.overlaps(scope_of(version)):
                continue
            try:
                await self.registry.version(conn, policy, version["id"])
            except (AccessDenied, NotReady):
                continue
            conflicts = await rows(
                conn,
                """SELECT * FROM presales.knowledge_versions WHERE document_id=:document
                AND id<>:id AND state IN ('candidate','awaiting_review')""",
                document=version["document_id"],
                id=version["id"],
            )
            if any(
                query.scope.overlaps(scope_of(v)) and scope_of(version).overlaps(scope_of(v))
                for v in conflicts
            ):
                raise Conflict()
            if version["build_state"] != "ready" or (
                version["model_identity"],
                version["dimensions"],
                version["mode"],
            ) != (self.embedding.model_identity, self.embedding.dimensions, self.embedding.mode):
                raise NotReady()
            allowed.append(version)
        signature = digest(
            [policy.authorization_revision, catalog, sorted(str(v["build_id"]) for v in allowed)]
        )
        return policy, catalog, allowed, signature

    def _authorize(self, query: EvidenceQuery, allowed: list[dict], *, rerank=False):
        adapter = self.reranker if rerank else self.embedding
        if adapter is not None:
            for declaration in {query.declaration.value, *(v["declaration"] for v in allowed)}:
                adapter.authorize(declaration)

    async def retrieve_evidence(self, actor: Actor, query: EvidenceQuery) -> RetrievalResult:
        self._authorize(query, [])
        identity = (self.embedding.model_identity, self.embedding.dimensions, self.embedding.mode)
        if identity[2] not in {"fake", "real"}:
            raise NotReady()
        async with self.database.transaction() as conn:
            policy, catalog, allowed, signature = await self._allowed(conn, actor, query)
            self._authorize(query, allowed)
            builds = [str(v["build_id"]) for v in allowed]
            candidates = await rows(
                conn,
                """SELECT c.*,l.locator FROM presales.chunks c
                JOIN presales.source_locations l ON l.id=c.source_location_id
                WHERE c.build_id=ANY(CAST(:builds AS uuid[])) ORDER BY c.id""",
                builds=builds,
            )
            key = digest(
                [
                    actor.subject,
                    signature,
                    query.model_dump(),
                    self.config.model_dump(),
                    identity,
                    self.reranker.identity if self.reranker else None,
                ]
            )
            cached = self._cache.get(key)
        degradation = []
        lexical = bm25(query.query, {v["id"]: tuple(v["tokens"]) for v in candidates})
        vector_rank: list[UUID] = []
        if cached is not None:
            ranked = cached["ranked"]
            degradation = cached["degradation"].copy()
            vector_rank, lexical = cached["vector"], cached["lexical"]
        elif candidates:
            vector = None
            try:
                values = await asyncio.wait_for(
                    self.embedding.embed([query.query]), self.config.embedding_seconds
                )
                if (
                    len(values) != 1
                    or len(values[0]) != identity[1]
                    or any(
                        isinstance(x, bool)
                        or not isinstance(x, (int, float))
                        or not math.isfinite(x)
                        for x in values[0]
                    )
                ):
                    raise EmbeddingFailure("invalid_query_vector")
                vector = values[0]
            except (TimeoutError, EmbeddingFailure):
                degradation.append("embedding_failed_keyword_only")
            async with self.database.transaction() as conn:
                _, _, current, current_signature = await self._allowed(conn, actor, query)
                if signature != current_signature:
                    raise Conflict()
                self._authorize(query, current)
                if vector is not None:
                    vector_rows = await rows(
                        conn,
                        """WITH allowed AS MATERIALIZED (
                        SELECT c.id,e.embedding FROM presales.chunks c
                        JOIN presales.embeddings e ON e.chunk_id=c.id
                        WHERE c.build_id=ANY(CAST(:builds AS uuid[])))
                        SELECT id FROM allowed ORDER BY
                        embedding <-> CAST(:vector AS public.vector),id LIMIT :limit""",
                        builds=builds,
                        vector=canonical(vector),
                        limit=self.config.per_route,
                    )
                    vector_rank = [v["id"] for v in vector_rows]
            ranked = rrf(
                [vector_rank, [i for i, _ in lexical[: self.config.per_route]]],
                self.config.rrf_constant,
            )
            if self.reranker and ranked:
                async with self.database.transaction() as conn:
                    _, _, current, current_signature = await self._allowed(conn, actor, query)
                    if current_signature != signature:
                        raise Conflict()
                    self._authorize(query, current, rerank=True)
                by_id = {v["id"]: v for v in candidates}
                try:
                    order = await asyncio.wait_for(
                        self.reranker.rank(
                            query.query, [by_id[i]["text_content"] for i, _ in ranked]
                        ),
                        self.config.reranker_seconds,
                    )
                    if (
                        len(order) != len(ranked)
                        or any(type(i) is not int for i in order)
                        or set(order) != set(range(len(ranked)))
                    ):
                        raise ValueError("Invalid reranking permutation")
                    ranked = [ranked[i] for i in order]
                except Exception:
                    degradation.append("reranker_failed_rrf_retained")
            elif not self.reranker:
                degradation.append("reranker_not_configured_rrf_retained")
        else:
            ranked = []
        if identity != (
            self.embedding.model_identity,
            self.embedding.dimensions,
            self.embedding.mode,
        ):
            raise Conflict()
        by_id = {v["id"]: v for v in candidates}
        if any(identifier not in by_id for identifier, _ in ranked):
            raise Conflict()
        async with self.database.transaction() as conn:
            policy, catalog, current, current_signature = await self._allowed(conn, actor, query)
            if current_signature != signature:
                raise Conflict()
            self._authorize(query, current)
            by_version = {version["id"]: version for version in current}
            selected = ranked[: self.config.limit]
            run_id = uuid4()
            await execute(
                conn,
                """INSERT INTO presales.retrieval_runs
                (id,actor,query_digest,authorization_revision,catalog_revision,mode,model_identity,
                 config,source_version_ids,ranking,degradation)
                VALUES(:id,:actor,:query,:auth,:catalog,:mode,:model,CAST(:config AS jsonb),
                 CAST(:sources AS jsonb),CAST(:ranking AS jsonb),CAST(:degradation AS jsonb))""",
                id=run_id,
                actor=actor.subject,
                query=digest(query.model_dump()),
                auth=policy.authorization_revision,
                catalog=catalog,
                mode=identity[2],
                model=identity[0],
                config=canonical(self.config.model_dump()),
                sources=canonical(sorted({str(v["source_version_id"]) for v in candidates})),
                ranking=canonical(
                    {"vector": vector_rank, "keyword": lexical, "selected": selected}
                ),
                degradation=canonical(degradation),
            )
            hits = [self._evidence(by_id[i], score, versions=by_version) for i, score in selected]
        if not degradation or degradation == ["reranker_not_configured_rrf_retained"]:
            self._cache[key] = {
                "ranked": ranked,
                "degradation": degradation.copy(),
                "vector": vector_rank,
                "lexical": lexical,
            }
            self._cache.move_to_end(key)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
        return RetrievalResult(
            run_id=run_id,
            mode=identity[2],
            model_identity=identity[0],
            authorization_revision=policy.authorization_revision,
            catalog_revision=catalog,
            cache_hit=cached is not None,
            degradation=degradation,
            hits=hits,
        )

    @staticmethod
    def _evidence(
        value: dict, score: float | None = None, *, versions: dict | None = None
    ) -> Evidence:
        version = (versions or {}).get(value["source_version_id"]) or {}
        return Evidence(
            evidence_id=value["id"],
            source_version_id=value["source_version_id"],
            build_id=value["build_id"],
            location=value["locator"],
            text=value["text_content"],
            start_offset=value["start_offset"],
            end_offset=value["end_offset"],
            score=score,
            declaration=version.get("declaration"),
            applicability=scope_of(version) if version else None,
        )

    async def read_evidence(self, actor: Actor, evidence_id: UUID) -> Evidence:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            await lock(conn, CATALOG_LOCK, shared=True)
            identities = await rows(
                conn,
                "SELECT source_version_id FROM presales.chunks WHERE id=:id",
                id=evidence_id,
            )
            if not identities:
                raise AccessDenied()
            version = await self.registry.version(conn, policy, identities[0]["source_version_id"])
            values = await rows(
                conn,
                """SELECT c.*,l.locator FROM presales.chunks c
                JOIN presales.source_locations l ON l.id=c.source_location_id
                JOIN presales.index_builds b ON b.id=c.build_id
                WHERE c.id=:id AND b.state='ready'""",
                id=evidence_id,
            )
            if not values:
                raise AccessDenied()
            return self._evidence(values[0], versions={version["id"]: version})

    async def read_run(self, actor: Actor, run_id: UUID) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            values = await rows(
                conn,
                "SELECT * FROM presales.retrieval_runs WHERE id=:id AND actor=:actor",
                id=run_id,
                actor=actor.subject,
            )
            if not values:
                raise AccessDenied()
            for version in values[0]["source_version_ids"]:
                await self.registry.version(conn, policy, UUID(version))
            return values[0]
