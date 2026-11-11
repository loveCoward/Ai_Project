"""Explicit, bounded real-Embedding development probe; never part of default pytest."""

import argparse
import asyncio
import json
import math
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from dotenv import dotenv_values

from presales.application.service import PresalesService
from presales.domain.contracts import Actor, SourceDeclaration, digest
from presales.indexing.embedding import ConfiguredEmbedding, EmbeddingFailure
from presales.infrastructure.config import DatabaseSettings, OutboundSettings
from presales.infrastructure.database import Database
from presales.infrastructure.files import LocalFiles
from presales.infrastructure.outbound import OutboundAdapter
from presales.knowledge.models import Applicability
from presales.maintenance.operator import current_operator
from presales.maintenance.service import AuthorizationChange, Maintenance, demo_id
from presales.retrieval.models import EvidenceQuery

SCOPE = Applicability(products=("synthetic-product",), deployments=("private",))
CONFIRMED_MODEL = "qwen3.7-text-embedding-flash"
CONFIRMED_ENDPOINT = "https://maas.qianwenaiapi.com/compatible-mode/v1/embeddings"
CONFIRMED_DIMENSIONS = 1024
PREFLIGHT_TEXT = "合成制造企业需要私有部署、审批工作流和操作审计。"
CREDENTIAL_NAMES = (
    "PRESALES_OUTBOUND_EMBEDDING__API_KEY",
    "EMBEDDING_API_KEY",
    "DASHSCOPE_API_KEY",
    "QWEN_API_KEY",
    "QIANWEN_API_KEY",
)
CORPUS = {
    "product": "合成制造企业AI平台：支持私有部署、审批工作流和操作审计。未承诺离线语音识别。",
    "security": "合成安全手册：审计日志默认保留30天，管理员可以导出审计记录。",
    "retention": "合成交付手册：项目交付约定审计日志默认保留90天，需要核对安全手册。",
    "risk": "合成部署说明：单节点模式不提供高可用；必须高可用的项目需要额外架构设计和验证。",
    "compatibility": "合成兼容性说明：已验证PostgreSQL 18和私有部署；其他数据库和部署条件待澄清。",
}
DISTRACTORS = (
    "合成品牌手册：展会海报使用绿色标题和灰色页脚。",
    "合成行政说明：办公室打印机每周检查纸张库存。",
    "合成活动说明：内部培训签到表由会务人员保管。",
)
CORPUS = {
    name: "\n\n".join([text, *(f"{name}：{v}" for v in DISTRACTORS)])
    for name, text in CORPUS.items()
}
DEV_CASES = (
    ("DEV-01", "complete", "支持哪些私有部署审批工作流能力？", ("product",)),
    ("DEV-02", "complete", "管理员如何导出操作审计记录？", ("security",)),
    ("DEV-03", "conflict", "审计日志默认保留30天还是90天？", ("security", "retention")),
    ("DEV-04", "conflict", "交付手册与安全手册的日志保留期是否一致？", ("security", "retention")),
    ("DEV-05", "unsupported", "能否承诺离线语音识别？", ("product",)),
    ("DEV-06", "unsupported", "是否支持尚未验证的数据库？", ("compatibility",)),
    ("DEV-07", "risk", "单节点可以满足必须高可用的要求吗？", ("risk",)),
    ("DEV-08", "risk", "高可用交付需要补充什么验证？", ("risk",)),
    ("DEV-09", "clarification", "客户没有说明数据库版本，需要澄清什么？", ("compatibility",)),
    (
        "DEV-10",
        "clarification",
        "客户部署条件未确定时有哪些已验证范围？",
        ("compatibility", "product"),
    ),
)


def dataset():
    return {
        "status": "development_draft_not_frozen_acceptance",
        "corpus": CORPUS,
        "cases": DEV_CASES,
    }


class BudgetedEmbedding:
    def __init__(self, model):
        self.model = model
        self.mode, self.dimensions, self.model_identity = (
            model.mode,
            model.dimensions,
            model.model_identity,
        )
        self.calls = self.characters = 0
        self.max_batch_items = 0
        self.call_records = []
        self.started = time.monotonic()

    def authorize(self, declaration):
        self.model.authorize(declaration)

    async def embed(self, texts):
        size = sum(len(text) for text in texts)
        if not 1 <= len(texts) <= 20 or any(not isinstance(text, str) for text in texts):
            raise EmbeddingFailure("probe_batch_limit")
        remaining = 600 - (time.monotonic() - self.started)
        if self.calls >= 20 or self.characters + size > 12000 or remaining <= 0:
            raise EmbeddingFailure("probe_budget_exhausted")
        self.calls += 1
        self.characters += size
        self.max_batch_items = max(self.max_batch_items, len(texts))
        record = {
            "request": self.calls,
            "items": len(texts),
            "characters": size,
            "status": "issued",
        }
        self.call_records.append(record)
        try:
            async with asyncio.timeout(min(60, remaining)):
                values = await self.model.embed(texts)
            if (
                not isinstance(values, list)
                or len(values) != len(texts)
                or any(
                    not isinstance(vector, list)
                    or len(vector) != self.dimensions
                    or any(
                        type(value) not in (int, float) or not math.isfinite(value)
                        for value in vector
                    )
                    for vector in values
                )
            ):
                raise EmbeddingFailure("probe_response_format_or_dimensions")
            record["status"] = "validated"
            return values
        except BaseException:
            record["status"] = "failed_or_inflight_unknown"
            raise


def confirmed_embedding():
    local_values = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    credential = next(
        (
            os.environ.get(name) or local_values.get(name)
            for name in CREDENTIAL_NAMES
            if os.environ.get(name) or local_values.get(name)
        ),
        None,
    )
    if not credential:
        raise EmbeddingFailure("embedding_credential_not_injected")
    values = {
        "mode": "real",
        "destination_alias": "user-confirmed-qwen-embedding",
        "endpoint": CONFIRMED_ENDPOINT,
        "service_identity": CONFIRMED_MODEL,
        "allowed_data_classes": frozenset({"synthetic"}),
        "api_key": credential,
    }
    outbound = OutboundAdapter(OutboundSettings(embedding=values))
    return ConfiguredEmbedding(outbound, CONFIRMED_DIMENSIONS, credential)


async def preflight(model):
    model.authorize("synthetic")
    await model.embed([PREFLIGHT_TEXT])
    return {
        "status": "passed",
        "response_model_identity": model.model_identity,
        "dimensions": model.dimensions,
        "vectors": 1,
        "format": "finite_numeric_array",
        "response_identity_check": "exact_provider_model_match",
    }


async def verify_embedding():
    report = {
        "mode": "real",
        "endpoint": CONFIRMED_ENDPOINT,
        "model_identity": CONFIRMED_MODEL,
        "dimensions": CONFIRMED_DIMENSIONS,
        "status": "not_executed",
        "requests": 0,
        "characters": 0,
        "d3_started": False,
        "cost": "unknown_no_amount_cap",
    }
    model = None
    try:
        model = BudgetedEmbedding(confirmed_embedding())
        report["preflight"] = await preflight(model)
        report["status"] = "preflight_passed_d3_not_started"
    except Exception as error:
        report["status"] = "preflight_blocked"
        report["error_code"] = (
            str(error) if isinstance(error, EmbeddingFailure) else type(error).__name__
        )
    finally:
        if model is not None:
            report.update(
                requests=model.calls, characters=model.characters, call_records=model.call_records
            )
    return report


async def discover():
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    endpoint = os.environ.get("COMPATIBLE_BASE_URL") or values.get("COMPATIBLE_BASE_URL")
    key = os.environ.get("COMPATIBLE_API_KEY") or values.get("COMPATIBLE_API_KEY")
    if not endpoint or not key:
        return {"status": "not_configured", "requests": 0}
    url = urlsplit(endpoint)
    if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
        return {"status": "unsafe_configuration", "requests": 0}
    result = {
        "purpose": "model_discovery_only_not_retrieval_probe",
        "endpoint_host": url.hostname,
        "requests": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
            response = await client.get(
                endpoint.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}
            )
            response.raise_for_status()
            ids = sorted(
                v["id"]
                for v in response.json()["data"]
                if isinstance(v.get("id"), str) and len(v["id"]) <= 200
            )
        return {
            **result,
            "status": "ok",
            "model_count": len(ids),
            "retrieval_model_candidates": [
                v for v in ids if any(t in v.lower() for t in ("embed", "bge", "rerank", "e5-"))
            ],
        }
    except Exception as error:
        return {**result, "status": "failed", "error_code": type(error).__name__}


async def execute_probe():
    if os.environ.get("PRESALES_ALLOW_DISPOSABLE_TESTS") != "yes":
        raise ValueError("Only the explicit disposable PostgreSQL runner is allowed")
    root = Path(os.environ["PRESALES_UI_FILE_ROOT"])
    admin = Database(DatabaseSettings(database_url=os.environ["PRESALES_TEST_URL"], file_root=root))
    runtime = Database(
        DatabaseSettings(database_url=os.environ["PRESALES_TEST_RUNTIME_URL"], file_root=root)
    )
    maintenance_db = Database(
        DatabaseSettings(database_url=os.environ["PRESALES_TEST_MAINTENANCE_URL"], file_root=root)
    )
    report = {
        "dataset_digest": digest(dataset()),
        "mode": "real",
        "results": [],
        "status": "not_executed",
        "cost": "unknown_no_amount_cap",
        "endpoint": CONFIRMED_ENDPOINT,
        "model_identity": CONFIRMED_MODEL,
        "dimensions": CONFIRMED_DIMENSIONS,
        "index_batch_limit": 20,
        "d3_started": False,
        "requests": 0,
        "characters": 0,
    }
    model = None
    try:
        from presales.application.workbench import Workbench
        from presales.knowledge.registry import KnowledgeRegistry
        from presales.retrieval.service import RetrievalService

        service = PresalesService(runtime, LocalFiles(root))
        model = BudgetedEmbedding(confirmed_embedding())
        report["preflight"] = await preflight(model)
        retrieval = RetrievalService(KnowledgeRegistry(service), model)
        workbench = Workbench(service, retrieval)
        workbench.index.batch_size = 20
        report["model_identity"] = model.model_identity
        report["dimensions"] = model.dimensions
        initializer = Maintenance(admin, {current_operator()})
        report["d3_started"] = True
        await initializer.initialize_demo()
        maintenance = Maintenance(maintenance_db, {current_operator()})
        architect, reviewer, sales = [
            Actor(subject=demo_id(role)) for role in ("architect", "reviewer", "sales")
        ]
        versions = {}
        for name, text in CORPUS.items():
            document = await workbench.registry.create_document(
                architect, f"Synthetic probe {name}", uuid4()
            )
            document_id = UUID(document["document_id"])
            preview = await maintenance.preview(
                [
                    AuthorizationChange(
                        kind="grant", principal_id=actor.subject, resource_id=document_id
                    )
                    for actor in (architect, reviewer, sales)
                ]
            )
            await maintenance.apply(
                UUID(preview["change_id"]),
                preview["digest"],
                "Synthetic disposable probe grants",
                confirm_expansion=True,
            )
            version = await workbench.registry.register_version(
                architect,
                document_id,
                f"{name}.md",
                text.encode(),
                SourceDeclaration.SYNTHETIC,
                SCOPE,
                uuid4(),
            )
            version_id = UUID(version["version_id"])
            parsed = await workbench.materials.parse_material(architect, version_id)
            if parsed["state"] != "succeeded":
                raise ValueError("probe_parse_failed")
            build = await workbench.index.build(architect, version_id)
            if build["state"] != "ready":
                raise ValueError("probe_index_failed")
            detail = await workbench.registry.inspect(architect, version_id)
            submitted = await workbench.review.submit(
                architect,
                version_id,
                UUID(build["build_id"]),
                detail["revision"],
                detail["catalog_revision"],
                uuid4(),
            )
            await workbench.review.decide(
                reviewer,
                UUID(submitted["review_id"]),
                True,
                "Synthetic probe independent publication",
                uuid4(),
            )
            versions[name] = str(version_id)
        for identifier, category, query, expected in DEV_CASES:
            try:
                value = await retrieval.retrieve_evidence(
                    sales,
                    EvidenceQuery(
                        query=query, scope=SCOPE, declaration=SourceDeclaration.SYNTHETIC
                    ),
                )
                missing = [
                    name
                    for name in expected
                    if not any(
                        str(hit.source_version_id) == versions[name]
                        and CORPUS[name].split("\n\n")[0] in hit.text
                        for hit in value.hits
                    )
                ]
                report["results"].append(
                    {
                        "id": identifier,
                        "category": category,
                        "outcome": "semantic_gap" if missing else "retrievable",
                        "missing_sources": missing,
                        "retrieval": value.model_dump(mode="json"),
                    }
                )
                if "embedding_failed_keyword_only" in value.degradation:
                    report["status"] = "basic_infeasible"
                    break
            except Exception as error:
                report["results"].append(
                    {
                        "id": identifier,
                        "category": category,
                        "outcome": "failed",
                        "error_code": type(error).__name__,
                    }
                )
                report["status"] = "basic_infeasible"
                break
        if report["status"] != "basic_infeasible":
            report["status"] = (
                "semantic_gap"
                if any(v["outcome"] == "semantic_gap" for v in report["results"])
                else "basic_chain_observed_not_acceptance"
            )
    except asyncio.CancelledError:
        report["status"] = "basic_infeasible"
        report["error_code"] = "probe_cancelled_or_deadline_exhausted"
        report["inflight_outcome"] = "unknown"
    except Exception as error:
        report["status"] = "configuration_or_setup_blocked"
        report["error_code"] = (
            str(error) if isinstance(error, EmbeddingFailure) else type(error).__name__
        )
    finally:
        if model is not None:
            report.update(
                requests=model.calls,
                characters=model.characters,
                max_batch_items=model.max_batch_items,
                call_records=model.call_records,
                elapsed_seconds=round(time.monotonic() - model.started, 3),
            )
        completed = {v["id"] for v in report["results"]}
        report["results"].extend(
            {"id": i, "category": c, "outcome": "not_executed_due_to_blocker"}
            for i, c, _, _ in DEV_CASES
            if i not in completed
        )
        await runtime.close()
        await maintenance_db.close()
        await admin.close()
    return report


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--discover-models", action="store_true")
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--execute", action="store_true")
    action.add_argument("--verify-embedding", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.prepare:
        report = {
            "dataset": dataset(),
            "digest": digest(dataset()),
            "status": "prepared_not_executed",
        }
    else:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            if args.discover_models:
                report = runner.run(discover())
            elif args.verify_embedding:
                report = runner.run(verify_embedding())
            else:

                async def bounded():
                    async with asyncio.timeout(600):
                        return await execute_probe()

                report = runner.run(bounded())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "results" and k != "dataset"},
            ensure_ascii=False,
        )
    )
    if report.get("status") in {
        "configuration_or_setup_blocked",
        "basic_infeasible",
        "failed",
        "preflight_blocked",
    }:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
