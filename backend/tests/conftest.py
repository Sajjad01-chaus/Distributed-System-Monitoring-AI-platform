"""Test wiring: SQLite database created by the real Alembic migration, and an in-memory
Redis (fakeredis) shared by the API and in-process workers."""
import os
import tempfile
from pathlib import Path

_DB = Path(tempfile.mkdtemp(prefix="dsm-test-")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ.update({"JWT_SECRET": "test-secret-" + "x" * 40, "ADMIN_USERNAME": "admin",
                   "ADMIN_PASSWORD": "admin-pw", "VIEWER_USERNAME": "viewer", "VIEWER_PASSWORD": "viewer-pw"})

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app import config as app_config  # noqa: E402
from app.pipeline import streams  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


def _migrate() -> None:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(cfg, "head")


_migrate()


@pytest.fixture
def redis_server(monkeypatch):
    """Fresh fake Redis per test; every client created through redis_factory shares it."""
    server = fakeredis.FakeServer()
    monkeypatch.setattr(streams, "redis_factory",
                        lambda: fakeredis.aioredis.FakeRedis(server=server, decode_responses=True))
    return server


@pytest.fixture
def redis_client(redis_server):
    return streams.redis_factory()


@pytest.fixture
def fast_claims(monkeypatch):
    """Treat any unacked entry as orphaned immediately (instead of after 30 s)."""
    monkeypatch.setattr(app_config, "CLAIM_IDLE_MS", 0)


@pytest.fixture(autouse=True)
def clean_tables():
    from app.database import engine
    from sqlalchemy import text
    yield
    with engine.begin() as conn:
        for table in ("system_metrics", "alerts", "agents"):
            conn.execute(text(f"DELETE FROM {table}"))


def token_headers(client, username="admin", password="admin-pw"):
    resp = client.post("/api/v1/auth/token", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
