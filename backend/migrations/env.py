"""Alembic environment: runs migrations against DATABASE_URL using the app's engine."""
from alembic import context

from app.database import Base, engine
import app.models  # noqa: F401  (registers tables on Base.metadata)

target_metadata = Base.metadata


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=connection.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
