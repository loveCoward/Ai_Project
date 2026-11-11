"""Serve a synthetic workbench inside the disposable PostgreSQL runner for UI checks."""

import argparse
import asyncio
import os
import secrets
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from presales.api.app import create_app
from presales.application.service import PresalesService
from presales.domain.contracts import SourceDeclaration
from presales.infrastructure.config import DatabaseSettings
from presales.infrastructure.database import Database
from presales.infrastructure.files import LocalFiles
from presales.maintenance.operator import current_operator
from presales.maintenance.service import AuthorizationChange, Maintenance, demo_id
from tests.presales.phase2a_helpers import ARCH, OPP, candidate, components

PASSWORD = "synthetic-browser-test-only"


async def seed(root: Path):
    admin = Database(DatabaseSettings(database_url=os.environ["PRESALES_TEST_URL"], file_root=root))
    runtime = Database(
        DatabaseSettings(database_url=os.environ["PRESALES_TEST_RUNTIME_URL"], file_root=root)
    )
    maintenance_db = Database(
        DatabaseSettings(database_url=os.environ["PRESALES_TEST_MAINTENANCE_URL"], file_root=root)
    )
    initializer = Maintenance(admin, {current_operator()})
    await initializer.initialize_demo()
    maintenance = Maintenance(maintenance_db, {current_operator()})
    service = PresalesService(runtime, LocalFiles(root))
    services = (service, maintenance, admin, initializer)
    registry, materials, index, review = components(service)
    document = await candidate(
        services,
        text=b"# Synthetic manufacturing AI platform\n\nPrivate deployment supports audit trails and workflow approvals.",
    )
    version = document[-1]
    # Knowledge publication is deliberately left for the two distinct browser roles.
    task = await service.create_task(
        ARCH, OPP, "Synthetic manufacturing platform proposal", uuid4()
    )
    task_id = UUID(task["resource_id"])
    material = await service.register_material(
        ARCH,
        task_id,
        "synthetic.md",
        b"# Synthetic requirement\n\nNeed private deployment and audit trails.",
        SourceDeclaration.SYNTHETIC,
        uuid4(),
        1,
    )
    print(
        f"Synthetic task {task_id}; candidate {version}; material {material['material_version_id']}",
        flush=True,
    )
    await runtime.close()
    await maintenance_db.close()
    await admin.close()
    return document[-2]


def configure(root: Path):
    os.environ.update(
        {
            "PRESALES_DATABASE_URL": os.environ["PRESALES_TEST_RUNTIME_URL"],
            "PRESALES_FILE_ROOT": str(root),
            "PRESALES_JWT_SECRET": secrets.token_hex(32),
            "PRESALES_JWT_ISSUER": "synthetic-browser-backend",
            "PRESALES_JWT_AUDIENCE": "synthetic-browser",
            "PRESALES_JWT_ACCESS_TOKEN_TTL_SECONDS": "60",
            "PRESALES_CATALOG_JWT_SECRET": secrets.token_hex(32),
            "PRESALES_OUTBOUND_EMBEDDING__MODE": "fake",
            "PRESALES_OUTBOUND_EMBEDDING__ALLOWED_DATA_CLASSES": '["synthetic"]',
            "PRESALES_RETRIEVAL_DIMENSIONS": "8",
        }
    )
    for role in ("sales", "architect", "reviewer"):
        os.environ[f"PRESALES_DEMO_{role.upper()}_USERNAME"] = role
        os.environ[f"PRESALES_DEMO_{role.upper()}_PASSWORD"] = PASSWORD


def app():
    application = create_app()
    dist = Path(__file__).resolve().parents[1] / "frontend/dist"
    application.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @application.get("/")
    @application.get("/login")
    @application.get("/workspace")
    async def frontend():
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-store"})

    return application


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if os.environ.get("PRESALES_ALLOW_DISPOSABLE_TESTS") != "yes":
        parser.error("Use the disposable PostgreSQL runner --workbench mode")
    root = Path(os.environ["PRESALES_UI_FILE_ROOT"]).resolve()
    configure(root)

    async def serve():
        document_id = await seed(root)
        control_file = root.parent / "ui-control"
        server = uvicorn.Server(
            uvicorn.Config(app(), host="127.0.0.1", port=args.port, log_level="warning")
        )

        async def controls():
            while not server.should_exit:
                if control_file.is_file():
                    command = control_file.read_text(encoding="utf-8").strip()
                    control_file.unlink()
                    if command == "stop":
                        server.should_exit = True
                    elif command == "revoke_sales_knowledge":
                        database = Database(
                            DatabaseSettings(
                                database_url=os.environ["PRESALES_TEST_MAINTENANCE_URL"],
                                file_root=root,
                            )
                        )
                        try:
                            maintenance = Maintenance(database, {current_operator()})
                            preview = await maintenance.preview(
                                [
                                    AuthorizationChange(
                                        kind="revoke",
                                        principal_id=demo_id("sales"),
                                        resource_id=document_id,
                                    )
                                ]
                            )
                            await maintenance.apply(
                                UUID(preview["change_id"]),
                                preview["digest"],
                                "Synthetic browser revocation",
                            )
                            print("Applied synthetic sales knowledge revocation", flush=True)
                        finally:
                            await database.close()
                await asyncio.sleep(0.2)

        monitor = asyncio.create_task(controls())
        print(
            f"Synthetic UI ready at http://127.0.0.1:{args.port}/login; explicit Fake mode; TTL=60s; pid={os.getpid()}; control={control_file}",
            flush=True,
        )
        try:
            await server.serve()
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(serve())


if __name__ == "__main__":
    main()
