from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    from backend.app import config
    from backend.app import db
    config.settings = config.Settings(database_url=f"sqlite:///{db_path.as_posix()}")
    db.engine.dispose()
    db.engine = __import__("sqlalchemy").create_engine(config.settings.database_url, connect_args={"check_same_thread": False})
    db.SessionLocal.configure(bind=db.engine)
    db.create_tables()
    from backend.seed import seed_database
    seed_database(reset=True, generate_cases_fn=None)
    from backend.app.main import app
    with TestClient(app) as test_client:
        yield test_client
