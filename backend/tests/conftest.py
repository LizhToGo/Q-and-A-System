from __future__ import annotations

import pytest
from fastapi.testclient import TestClient



@pytest.fixture(scope="session", autouse=True)
def real_rag_engine():
    from pathlib import Path
    import subprocess

    from backend.app.config import ROOT_DIR, settings
    from backend.app.services.rag_worker import close_workers, worker_environment

    artifact = Path(settings.rag_engine_path)
    if not artifact.is_file():
        subprocess.run(
            [settings.rag_dotnet_executable, "publish", str(ROOT_DIR / "rag-engine" / "Rag.Search" / "Rag.Search.csproj"),
             "-c", "Release", "-o", str(artifact.parent)],
            cwd=ROOT_DIR, env=worker_environment(), check=True, timeout=180,
        )
    yield
    close_workers()

@pytest.fixture(autouse=True)
def isolate_runtime_provider_files(tmp_path, monkeypatch):
    from backend.app import provider_config

    monkeypatch.setattr(provider_config, "PROVIDER_CONFIG_PATH", tmp_path / "provider-config.json")
    monkeypatch.setattr(provider_config, "ADMIN_TOKEN_PATH", tmp_path / "admin-token")

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
