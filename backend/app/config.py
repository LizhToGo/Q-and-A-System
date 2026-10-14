from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DATABASE_PATH = DATA_DIR / "knowledge.db"
ENV_PATH = ROOT_DIR / ".env"

def _load_local_env(path: Path) -> None:
    """Load local defaults; only non-empty process variables take precedence."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and not os.environ.get(key):
            os.environ[key] = value


_load_local_env(ENV_PATH)
@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{DATABASE_PATH.as_posix()}")
    llm_provider: str = os.getenv("LLM_PROVIDER", "none")
    llm_base_url: str | None = os.getenv("LLM_BASE_URL")
    llm_api_key: str | None = os.getenv("LLM_API_KEY")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-v4.1-flash")
    llm_gateway_messages: bool = os.getenv("LLM_GATEWAY_MESSAGES", "false").lower() == "true"
    llm_config_source: str = os.getenv("LLM_CONFIG_SOURCE", "saved").lower()
    web_search_enabled: bool = os.getenv("WEB_SEARCH_ENABLED", "false").lower() == "true"
    web_search_url: str | None = os.getenv("WEB_SEARCH_URL")
    web_search_api_key: str | None = os.getenv("WEB_SEARCH_API_KEY")


settings = Settings()
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
