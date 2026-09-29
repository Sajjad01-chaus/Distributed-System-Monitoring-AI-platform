import os
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import declarative_base, sessionmaker

# No default: credentials come from the environment (.env via docker compose), never from code.
def _normalize(url: str) -> str:
    """Hosting providers hand out postgres:// or postgresql:// URLs. SQLAlchemy 2.1 maps the bare
    scheme to psycopg (v3), which isn't installed, so name the driver we ship explicitly."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


DATABASE_URL = _normalize(os.environ["DATABASE_URL"])

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
