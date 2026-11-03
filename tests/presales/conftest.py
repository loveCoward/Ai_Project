import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from presales.application.service import PresalesService
from presales.infrastructure.config import DatabaseSettings
from presales.infrastructure.database import Database
from presales.infrastructure.files import LocalFiles
from presales.maintenance.operator import current_operator
from presales.maintenance.service import Maintenance


def pytest_configure(config):
    config.addinivalue_line("markers", "postgres: explicit disposable PostgreSQL integration tests")


def pytest_asyncio_loop_factories():
    return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture(scope="session")
def test_url():
    value = os.environ.get("PRESALES_TEST_URL")
    if not value or os.environ.get("PRESALES_ALLOW_DISPOSABLE_TESTS") != "yes":
        pytest.skip("Explicit disposable PostgreSQL URL and opt-in required")
    url = make_url(value)
    if url.database != "biddesk_phase1_test" or url.host not in {"localhost", "127.0.0.1"}:
        pytest.fail("Refusing to migrate or reset a non-dedicated test database")
    engine = create_engine(value, hide_parameters=True)
    with engine.connect() as conn:
        marker = conn.execute(text("SELECT value FROM public.phase_one_test_marker")).scalar_one()
        assert marker == "disposable-phase-one"
        cfg = Config(str(Path("alembic.ini").resolve()))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001_identity")
        conn.commit()
        command.upgrade(cfg, "head")
        conn.commit()
        command.upgrade(cfg, "head")
        conn.commit()
    engine.dispose()
    return value


@pytest_asyncio.fixture
async def services(test_url, tmp_path):
    engine = create_engine(test_url, hide_parameters=True)
    with engine.begin() as conn:
        conn.execute(
            text("""TRUNCATE iam.principals,iam.initialization,iam.authorization_changes,
            presales.resources,audit.events RESTART IDENTITY CASCADE""")
        )
        conn.execute(text("UPDATE iam.authorization_revision SET revision=0"))
    admin = Database(DatabaseSettings(database_url=test_url, file_root=tmp_path))
    operator = current_operator()
    initializer = Maintenance(admin, {operator})
    await initializer.initialize_demo()
    runtime = Database(
        DatabaseSettings(database_url=os.environ["PRESALES_TEST_RUNTIME_URL"], file_root=tmp_path)
    )
    maint_db = Database(
        DatabaseSettings(
            database_url=os.environ["PRESALES_TEST_MAINTENANCE_URL"], file_root=tmp_path
        )
    )
    maintenance = Maintenance(maint_db, {operator})
    service = PresalesService(runtime, LocalFiles(tmp_path))
    yield service, maintenance, admin, initializer
    await runtime.close()
    await maint_db.close()
    await admin.close()
    engine.dispose()
