from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import config as app_config

PROVIDER_CONFIG_PATH = app_config.DATA_DIR / "provider-config.json"
ADMIN_TOKEN_PATH = app_config.DATA_DIR / "admin-token"


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    config_source: str
    enabled: bool
    provider: str
    base_url: str
    model: str
    models: list[str] = field(default_factory=list)
    api_key: str = field(default="", repr=False)
    gateway_messages: bool = False


def _normalize_base_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain userinfo")
    if "?" in str(value) or "#" in str(value):
        raise ValueError("base_url must not contain query or fragment")
    parsed.port  # validates malformed or out-of-range ports
    path = parsed.path.rstrip("/")
    if "/chat/completions" in path.lower():
        raise ValueError("Chat Completions endpoints are not supported")
    if not path.endswith("/responses"):
        path += "/responses" if path.endswith("/v1") else "/v1/responses"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _models(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in result:
            result.append(item)
    return result


def _from_mapping(data: Mapping[str, Any], *, provider_id: str, source: str) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        config_source=source,
        enabled=bool(data.get("enabled", False)),
        provider=str(data.get("provider", "none") or "none"),
        base_url=_normalize_base_url(data.get("base_url", "") or ""),
        model=str(data.get("model", "") or ""),
        models=_models(data.get("models", [])),
        api_key=str(data.get("api_key", "") or ""),
        gateway_messages=bool(data.get("gateway_messages", False)),
    )


def _config_source(defaults: Any) -> str | None:
    """Return an explicitly configured source, or None when it was omitted."""
    if not hasattr(defaults, "llm_config_source"):
        return None
    source = str(getattr(defaults, "llm_config_source", "") or "").strip().lower()
    return source if source in {"environment", "saved"} else None


def _environment(defaults: Any) -> ProviderConfig:
    provider = getattr(defaults, "llm_provider", "none") or "none"
    gateway = getattr(defaults, "llm_gateway_messages", os.getenv("LLM_GATEWAY_MESSAGES", "false"))
    api_key = getattr(defaults, "llm_api_key", None) or ""
    if api_key.strip().lower() in {"replace-with-new-token", "replace_with_new_token"}:
        api_key = ""
    return ProviderConfig(
        id="environment", config_source="environment", enabled=str(provider).lower() != "none",
        provider=str(provider), base_url=_normalize_base_url(getattr(defaults, "llm_base_url", None) or ""),
        model=getattr(defaults, "llm_model", "") or "", models=[], api_key=api_key,
        gateway_messages=gateway if isinstance(gateway, bool) else str(gateway).lower() == "true",
    )


def _read_registry() -> tuple[list[dict[str, Any]], str | None]:
    """Read both the current registry and the original single-object format.

    Profile dictionaries deliberately retain unknown keys so migration and future
    settings never discard data that is not currently rendered by the API.
    """
    if not PROVIDER_CONFIG_PATH.exists():
        return [], None
    try:
        with PROVIDER_CONFIG_PATH.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return [], None
    if not isinstance(payload, dict):
        return [], None
    profiles = payload.get("providers")
    if isinstance(profiles, list):
        rows = [dict(item) for item in profiles if isinstance(item, dict)]
        default = payload.get("default_provider_id")
        return rows, str(default) if isinstance(default, str) and default else None
    # Legacy provider-config.json was one profile object. Keep every field.
    legacy = dict(payload)
    legacy.setdefault("id", "saved-default")
    return [legacy], "saved-default"


def _registry_rows() -> tuple[list[dict[str, Any]], str | None]:
    rows, default = _read_registry()
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["id"] = str(item.get("id") or "saved-default")
        normalized.append(item)
    if default and default != "environment" and not any(row["id"] == default for row in normalized):
        default = None
    return normalized, default


def _registry_default_explicit() -> bool:
    if not PROVIDER_CONFIG_PATH.exists():
        return False
    try:
        with PROVIDER_CONFIG_PATH.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        return bool(isinstance(payload, dict) and payload.get("default_provider_explicit", False))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _write_registry(rows: list[dict[str, Any]], default_provider_id: str | None,
                    *, explicit_default: bool | None = None) -> None:
    PROVIDER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"providers": rows}
    if default_provider_id:
        payload["default_provider_id"] = default_provider_id
    if explicit_default is None:
        explicit_default = _registry_default_explicit()
    if explicit_default:
        payload["default_provider_explicit"] = True
    fd, temporary = tempfile.mkstemp(prefix=".provider-config.", dir=PROVIDER_CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, PROVIDER_CONFIG_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _saved_configs() -> list[ProviderConfig]:
    rows, _ = _registry_rows()
    result: list[ProviderConfig] = []
    for row in rows:
        result.append(_from_mapping(row, provider_id=str(row["id"]), source="saved"))
    return result


def _default_id(defaults: Any, *, profiles: list[ProviderConfig], registry_default: str | None,
                explicit_default: bool = False) -> str:
    source = _config_source(defaults)
    if source == "environment" and not explicit_default:
        return "environment"
    if registry_default == "environment":
        return "environment"
    if registry_default and any(item.id == registry_default and item.enabled for item in profiles):
        return registry_default
    for item in profiles:
        if item.enabled and item.api_key and item.base_url:
            return item.id
    return "environment"


def get_provider_config(defaults: Any = None, *, provider_id: str | None = None,
                        model: str | None = None) -> ProviderConfig:
    defaults = app_config.settings if defaults is None else defaults
    environment = _environment(defaults)
    profiles = _saved_configs()
    _, registry_default = _registry_rows()
    selected_id = provider_id or _default_id(
        defaults, profiles=profiles, registry_default=registry_default,
        explicit_default=_registry_default_explicit())
    if selected_id == "environment":
        if provider_id is not None and not environment.enabled:
            raise ValueError("provider is disabled")
        selected = environment
    else:
        selected = next((item for item in profiles if item.id == selected_id), None)
        if selected is None:
            raise ValueError("unknown provider")
        if not selected.enabled:
            raise ValueError("provider is disabled")
    if model is not None:
        selected = ProviderConfig(**{**selected.__dict__, "model": str(model)})
    return selected


def _public_profile(cfg: ProviderConfig) -> dict[str, Any]:
    return {
        "id": cfg.id, "enabled": cfg.enabled, "provider": cfg.provider,
        "base_url": cfg.base_url, "model": cfg.model, "models": list(cfg.models),
        "protocol": "responses", "gateway_messages": cfg.gateway_messages,
        "api_key_configured": bool(cfg.api_key), "config_source": cfg.config_source,
    }


def list_admin_providers(defaults: Any = None) -> dict[str, Any]:
    defaults = app_config.settings if defaults is None else defaults
    saved = _saved_configs()
    environment = _environment(defaults)
    _, registry_default = _registry_rows()
    default_id = _default_id(
        defaults, profiles=saved, registry_default=registry_default,
        explicit_default=_registry_default_explicit())
    return {"providers": [_public_profile(environment), *(_public_profile(item) for item in saved)],
            "default_provider_id": default_id, "env_path": str(app_config.ENV_PATH)}


def list_public_providers(defaults: Any = None) -> dict[str, Any]:
    catalog = list_admin_providers(defaults)
    usable = [
        {key: item[key] for key in ("id", "provider", "model", "models", "enabled", "api_key_configured")}
        for item in catalog["providers"] if item["enabled"] and item["api_key_configured"] and item["base_url"]
    ]
    default = catalog["default_provider_id"]
    if not any(item["id"] == default for item in usable):
        default = usable[0]["id"] if usable else "environment"
    return {"providers": usable, "default_provider_id": default}
def _validate_values(values: dict[str, Any]) -> ProviderConfig:
    cfg = _from_mapping(values, provider_id=str(values.get("id", "saved-default")), source="saved")
    if cfg.enabled and not cfg.base_url:
        raise ValueError("enabled provider requires a base_url")
    return cfg


def save_provider_config(update: dict[str, Any], *, provider_id: str | None = None,
                         copy_from: str | None = None) -> dict[str, Any]:
    """Create or update one saved profile, retaining omitted credentials."""
    rows, default = _registry_rows()
    target_id = provider_id or str(update.get("id") or "")
    if not target_id:
        target_id = "custom-" + uuid.uuid4().hex[:12]
    existing = next((row for row in rows if str(row.get("id")) == target_id), None)
    if provider_id is not None and existing is None:
        raise ValueError("unknown provider")
    values = dict(existing or {})
    values.update({key: update[key] for key in ("enabled", "provider", "base_url", "model", "models", "gateway_messages") if key in update})
    if "api_key" in update and update.get("api_key") not in (None, ""):
        values["api_key"] = str(update["api_key"])
    elif existing is None and copy_from:
        source = _environment(app_config.settings) if copy_from == "environment" else next((row for row in rows if row.get("id") == copy_from), None)
        if source is None:
            raise ValueError("unknown copy source")
        values["api_key"] = source.api_key if isinstance(source, ProviderConfig) else str(source.get("api_key", ""))
    elif "api_key" not in values:
        values["api_key"] = ""
    values["id"] = target_id
    cfg = _validate_values(values)
    values["base_url"] = cfg.base_url
    values["models"] = cfg.models
    if existing is None:
        rows.append(values)
    else:
        existing.clear()
        existing.update(values)
    if default is None:
        default = target_id
    _write_registry(rows, default)
    return _public_profile(cfg)


def set_default_provider(provider_id: str) -> str:
    if provider_id == "environment":
        if not _environment(app_config.settings).enabled:
            raise ValueError("provider is disabled")
    else:
        cfg = next((item for item in _saved_configs() if item.id == provider_id), None)
        if cfg is None:
            raise ValueError("unknown provider")
        if not cfg.enabled:
            raise ValueError("provider is disabled")
    rows, _ = _registry_rows()
    _write_registry(rows, provider_id, explicit_default=True)
    return provider_id


def _config_for_models(provider_id: str | None = None, draft: Mapping[str, Any] | None = None) -> ProviderConfig:
    if draft is None:
        return get_provider_config(provider_id=provider_id)
    draft = dict(draft)
    selected_id = provider_id or draft.get("provider_id")
    # Drafts never silently borrow the currently selected/default profile.
    key = draft.get("api_key")
    if key in (None, "") and selected_id:
        key = get_provider_config(provider_id=str(selected_id)).api_key
    if key in (None, ""):
        key = ""
    values = {"id": str(selected_id or "preview"), "enabled": bool(draft.get("enabled", True)),
              "provider": draft.get("provider", "custom"), "base_url": draft.get("base_url", ""),
              "model": draft.get("model", ""), "models": draft.get("models", []),
              "api_key": key, "gateway_messages": draft.get("gateway_messages", False)}
    cfg = _validate_values(values)
    if not cfg.enabled:
        raise ValueError("provider is disabled")
    return cfg


async def list_provider_models(provider_id: str | None = None, draft: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = _config_for_models(provider_id, draft)
    if not cfg.api_key or not cfg.base_url:
        raise ValueError("provider URL and API key are required")
    url = cfg.base_url.removesuffix("/responses") + "/models"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {cfg.api_key}"})
            response.raise_for_status()
            payload = response.json()
        rows = payload["data"]
        if not isinstance(rows, list):
            raise ValueError("invalid models response")
        found: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            model_id = row.get("id") if isinstance(row, dict) else None
            if not isinstance(model_id, str) or not model_id or model_id in seen:
                continue
            if cfg.api_key and cfg.api_key in model_id or any(ord(ch) < 32 for ch in model_id):
                continue
            seen.add(model_id)
            found.append({"id": model_id})
        return {"models": found, "current_model": cfg.model}
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise RuntimeError("provider model listing unavailable") from None


def get_admin_token() -> str:
    configured = os.getenv("ADMIN_API_TOKEN")
    if configured:
        return configured
    ADMIN_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
        raise ValueError("admin token file is empty")
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    fd, temporary = tempfile.mkstemp(prefix=".admin-token.", dir=ADMIN_TOKEN_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, ADMIN_TOKEN_PATH)
        except FileExistsError:
            token = ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
    finally:
        os.unlink(temporary)
    return token


def admin_token_matches(candidate: str | None) -> bool:
    return hmac.compare_digest((candidate or "").encode("utf-8"), get_admin_token().encode("utf-8"))
