from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.app import provider_config


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"id": "model-a"}, {"id": "model-a"}, {"id": "model-b"}]}


class FakeAsyncClient:
    calls: list[tuple[str, dict, dict]] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs.get("headers", {}), kwargs))
        return FakeResponse()


def _setup(monkeypatch, tmp_path, *, source=None):
    monkeypatch.setattr(provider_config, "PROVIDER_CONFIG_PATH", tmp_path / "provider-config.json")
    monkeypatch.setattr(provider_config, "ADMIN_TOKEN_PATH", tmp_path / "admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-admin-token")
    values = {
        "llm_provider": "openai_compatible",
        "llm_base_url": "https://gateway.example/v1/responses",
        "llm_api_key": "environment-key",
        "llm_model": "model-env",
        "llm_gateway_messages": False,
    }
    if source is not None:
        values["llm_config_source"] = source
    monkeypatch.setattr(provider_config.app_config, "settings", SimpleNamespace(**values))


def _headers():
    return {"X-Admin-Token": "test-admin-token"}


def _profile(**overrides):
    values = {
        "enabled": True,
        "provider": "custom",
        "base_url": "https://gateway.example/v1",
        "model": "model-a",
        "models": ["model-a"],
        "gateway_messages": False,
        "api_key": "profile-key",
    }
    values.update(overrides)
    return values


def test_provider_admin_requires_token_and_sanitizes_catalog(client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert client.get("/api/admin/providers").status_code == 401
    response = client.get("/api/admin/providers", headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["providers"][0]["id"] == "environment"
    assert all("api_key" not in profile for profile in body["providers"])
    assert "environment-key" not in response.text
    assert body["providers"][0]["config_source"] == "environment"


def test_profiles_coexist_and_omitted_key_stays_isolated(client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    first = client.post("/api/admin/providers", headers=_headers(), json=_profile()).json()
    second = client.post("/api/admin/providers", headers=_headers(), json=_profile(
        provider="second", base_url="https://second.example/v1", api_key="second-key" )).json()
    assert first["id"] != second["id"]
    update = _profile(model="model-updated")
    update.pop("api_key")
    assert client.put(f"/api/admin/providers/{first['id']}", headers=_headers(), json=update).status_code == 200
    assert provider_config.get_provider_config(provider_id=first["id"]).api_key == "profile-key"
    assert provider_config.get_provider_config(provider_id=second["id"]).api_key == "second-key"
    saved = json.loads((tmp_path / "provider-config.json").read_text(encoding="utf-8"))
    assert len(saved["providers"]) == 2


def test_legacy_single_profile_migrates_without_losing_settings(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    legacy = {"enabled": True, "provider": "legacy", "base_url": "https://legacy.example/v1",
              "model": "legacy-model", "api_key": "legacy-key", "gateway_messages": True,
              "custom_setting": {"keep": True}}
    (tmp_path / "provider-config.json").write_text(json.dumps(legacy), encoding="utf-8")
    cfg = provider_config.get_provider_config()
    assert cfg.id == "saved-default"
    assert cfg.api_key == "legacy-key"
    assert provider_config._read_registry()[0][0]["custom_setting"] == {"keep": True}


def test_model_listing_uses_selected_profile_and_deduplicates(client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    profile = client.post("/api/admin/providers", headers=_headers(), json=_profile()).json()
    monkeypatch.setattr(provider_config.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.calls = []
    response = client.get(f"/api/admin/providers/{profile['id']}/models", headers=_headers())
    assert response.status_code == 200
    assert response.json() == {"models": [{"id": "model-a"}, {"id": "model-b"}], "current_model": "model-a"}
    assert FakeAsyncClient.calls[0][0] == "https://gateway.example/v1/models"
    assert FakeAsyncClient.calls[0][1]["Authorization"].startswith("Bearer ")


def test_model_preview_uses_unsaved_url_and_key(client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    saved = client.post("/api/admin/providers", headers=_headers(), json=_profile(
        base_url="https://active.example/v1", api_key="active-key")).json()
    monkeypatch.setattr(provider_config.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.calls = []
    response = client.post("/api/admin/providers/models", headers=_headers(), json={
        "provider_id": saved["id"], "enabled": True, "provider": "draft",
        "base_url": "https://draft.example/v1", "model": "draft-model",
        "models": [], "gateway_messages": False, "api_key": "draft-key",
    })
    assert response.status_code == 200
    assert FakeAsyncClient.calls[0][0] == "https://draft.example/v1/models"
    assert "draft-key" not in response.text


def test_source_precedence_and_explicit_default(client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, source="environment")
    saved = client.post("/api/admin/providers", headers=_headers(), json=_profile()).json()
    assert provider_config.get_provider_config().id == "environment"
    assert client.put("/api/admin/providers/default", headers=_headers(), json={"provider_id": saved["id"]}).status_code == 200
    assert provider_config.get_provider_config().id == saved["id"]
    assert client.put("/api/admin/providers/default", headers=_headers(), json={"provider_id": "environment"}).status_code == 200
    assert provider_config.get_provider_config().id == "environment"


def test_public_catalog_excludes_disabled_and_unknown_selection_is_rejected(client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    enabled = client.post("/api/admin/providers", headers=_headers(), json=_profile()).json()
    client.post("/api/admin/providers", headers=_headers(), json=_profile(enabled=False, api_key="disabled-key"))
    public = client.get("/api/providers")
    assert public.status_code == 200
    assert all(set(item) == {"id", "provider", "model", "models", "enabled", "api_key_configured"}
               for item in public.json()["providers"])
    assert enabled["id"] in {item["id"] for item in public.json()["providers"]}
    assert client.get("/api/providers/unknown/models").status_code == 422
    assert client.get("/api/providers/unknown/models").text.find("unknown") == -1


def test_model_override_is_request_local(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    provider_config.save_provider_config(_profile())
    cfg = provider_config.get_provider_config(model="temporary-model")
    assert cfg.model == "temporary-model"
    assert provider_config.get_provider_config().model == "model-a"


def test_copying_key_is_explicit_and_blank_updates_preserve_key(client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    values = _profile(api_key="")
    standalone = client.post("/api/admin/providers", headers=_headers(), json=values).json()
    assert provider_config.get_provider_config(provider_id=standalone["id"]).api_key == ""
    copied = client.post("/api/admin/providers", headers=_headers(), json={**values, "copy_from": "environment"}).json()
    assert provider_config.get_provider_config(provider_id=copied["id"]).api_key == "environment-key"
    assert client.put(f"/api/admin/providers/{copied['id']}", headers=_headers(), json=values).status_code == 200
    assert provider_config.get_provider_config(provider_id=copied["id"]).api_key == "environment-key"
    assert client.put("/api/admin/providers/environment", headers=_headers(), json=values).status_code == 403


def test_ask_rejects_unknown_or_disabled_provider_instead_of_using_default(client, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    disabled = client.post("/api/admin/providers", headers=_headers(), json=_profile(enabled=False)).json()
    for provider_id in ("unknown", disabled["id"]):
        response = client.post("/api/answers/ask", json={"question": "采样定理", "provider_id": provider_id})
        assert response.status_code == 400
        assert "profile-key" not in response.text
