import os
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import declarative_base, sessionmaker

# No default: credentials come from the environment (.env via docker compose), never from code.
DATABASE_URL = os.environ["DATABASE_URL"]

# SQLite (used by the test suite) needs cross-thread access for FastAPI's threadpool, and a busy
# timeout because API and workers write from separate processes.
connect_args = {"check_same_thread": False, "timeout": 30} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def upsert(table):
    """INSERT that supports .on_conflict_do_nothing()/.on_conflict_do_update() on both dialects."""
    dialect = postgresql if engine.dialect.name == "postgresql" else sqlite
    return dialect.insert(table)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
