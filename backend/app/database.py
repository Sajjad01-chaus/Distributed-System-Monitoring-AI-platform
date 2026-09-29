import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# No default: credentials come from the environment (.env via docker compose), never from code.
DATABASE_URL = os.environ["DATABASE_URL"]

# SQLite (used by the test suite) needs cross-thread access for FastAPI's threadpool.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
