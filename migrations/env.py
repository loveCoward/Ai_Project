import os

from alembic import context
from sqlalchemy import create_engine, pool


def migrate(connection):
    context.configure(connection=connection, version_table="presales_migration_version")
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    migrate(connection)
else:
    url = os.environ.get("PRESALES_MIGRATION_URL")
    if not url or not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("Set an explicit PostgreSQL migration URL; no .env is loaded")
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        migrate(connection)
    engine.dispose()
