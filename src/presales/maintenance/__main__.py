import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from presales.application.service import PresalesService, audit
from presales.domain.contracts import Actor, DomainError
from presales.infrastructure.config import DatabaseSettings
from presales.infrastructure.database import Database, rows
from presales.infrastructure.files import LocalFiles
from presales.maintenance.operator import current_operator, require_operator
from presales.maintenance.service import AuthorizationChange, Maintenance


async def run(args) -> dict:
    allowed = set(filter(None, os.environ.get("PRESALES_OPERATOR_ALLOWLIST", "").split(",")))
    operator = require_operator(allowed)
    key = "PRESALES_BOOTSTRAP_URL" if args.command == "initialize" else "PRESALES_MAINTENANCE_URL"
    if args.command == "readiness":
        key = "PRESALES_DATABASE_URL"
    url = os.environ.get(key)
    if not url:
        raise ValueError("Set the connection appropriate to this explicit command")
    database = Database(DatabaseSettings(database_url=url))
    try:
        maintenance = Maintenance(database, allowed)
        if args.command == "initialize":
            return await maintenance.initialize_demo()
        if args.command == "authorization":
            if args.action == "preview":
                changes = json.loads(Path(args.file).read_text(encoding="utf-8"))
                return await maintenance.preview(
                    [AuthorizationChange.model_validate(c) for c in changes]
                )
            if args.action == "apply":
                return await maintenance.apply(
                    args.id, args.digest, args.reason, confirm_expansion=args.confirm_expansion
                )
            return await maintenance.status(args.id)
        async with database.transaction() as conn:
            versions = await rows(
                conn, "SELECT created_by FROM presales.resource_versions WHERE id=:id", id=args.id
            )
            if not versions:
                raise ValueError("Unknown readiness record")
        service = PresalesService(database, LocalFiles(database.settings.file_root))
        await service.reconcile_material(Actor(subject=versions[0]["created_by"]), args.id)
        async with database.transaction() as conn:
            revision = (await rows(conn, "SELECT revision FROM iam.authorization_revision"))[0][
                "revision"
            ]
            await audit(
                conn, operator, "operator_readiness_reconcile", args.id, "success", revision
            )
        return {"readiness_id": str(args.id), "status": "verified"}
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explicit local presales maintenance; never a model tool"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("operator-id")
    commands.add_parser("initialize")
    authorization = commands.add_parser("authorization").add_subparsers(
        dest="action", required=True
    )
    preview = authorization.add_parser("preview")
    preview.add_argument("--file", required=True)
    apply = authorization.add_parser("apply")
    apply.add_argument("--id", type=UUID, required=True)
    apply.add_argument("--digest", required=True)
    apply.add_argument("--reason", required=True)
    apply.add_argument("--confirm-expansion", action="store_true")
    status = authorization.add_parser("status")
    status.add_argument("--id", type=UUID, required=True)
    readiness = commands.add_parser("readiness").add_subparsers(dest="action", required=True)
    reconcile = readiness.add_parser("reconcile")
    reconcile.add_argument("--id", type=UUID, required=True)
    args = parser.parse_args()
    if args.command == "operator-id":
        print(current_operator())
        return
    try:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            result = runner.run(run(args))
    except DomainError as error:
        parser.exit(1, f"{error.code}\n")
    except Exception:
        parser.exit(1, "Maintenance failed; check explicit configuration without logging secrets\n")
    print(json.dumps(result, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
