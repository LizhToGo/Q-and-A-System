from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import provider_config
from .provider_config import admin_token_matches

router = APIRouter()


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    provider: str
    base_url: str
    model: str
    models: list[str] = Field(default_factory=list)
    gateway_messages: bool
    api_key: str | None = None


class ProviderCreate(ProviderUpdate):
    copy_from: str | None = None


class ProviderDefault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str


class ProviderModelsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str | None = None
    enabled: bool = True
    provider: str = "custom"
    base_url: str = ""
    model: str = ""
    models: list[str] = Field(default_factory=list)
    gateway_messages: bool = False
    api_key: str | None = None


def _auth(token: str | None) -> None:
    if not admin_token_matches(token):
        raise HTTPException(status_code=401, detail="unauthorized")


def _invalid(exc: Exception) -> HTTPException:
    # Do not expose URLs, keys, or provider payloads in API errors.
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail="invalid provider configuration")
    return HTTPException(status_code=500, detail="provider configuration unavailable")


@router.get("/api/admin/providers")
def get_providers(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    _auth(x_admin_token)
    try:
        return provider_config.list_admin_providers()
    except Exception as exc:
        raise _invalid(exc) from None


@router.post("/api/admin/providers")
def post_provider(update: ProviderCreate, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    _auth(x_admin_token)
    try:
        payload = update.model_dump(exclude_none=True)
        copy_from = payload.pop("copy_from", None)
        return provider_config.save_provider_config(payload, copy_from=copy_from)
    except Exception as exc:
        raise _invalid(exc) from None


@router.put("/api/admin/providers/default")
def put_default(update: ProviderDefault,
                x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    _auth(x_admin_token)
    try:
        return {"default_provider_id": provider_config.set_default_provider(update.provider_id)}
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid provider selection") from None
    except Exception:
        raise HTTPException(status_code=500, detail="provider configuration unavailable") from None
@router.put("/api/admin/providers/{provider_id}")
def put_provider(provider_id: str, update: ProviderUpdate,
                 x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    _auth(x_admin_token)
    if provider_id == "environment":
        raise HTTPException(status_code=403, detail="environment provider is read-only")
    try:
        return provider_config.save_provider_config(update.model_dump(exclude_none=True), provider_id=provider_id)
    except Exception as exc:
        raise _invalid(exc) from None




@router.post("/api/admin/providers/models")
async def post_provider_models(update: ProviderModelsDraft,
                               x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    _auth(x_admin_token)
    try:
        return await provider_config.list_provider_models(update.provider_id, update.model_dump(exclude_none=True))
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid provider configuration") from None
    except RuntimeError:
        raise HTTPException(status_code=502, detail="provider model listing unavailable") from None
    except Exception:
        raise HTTPException(status_code=502, detail="provider model listing unavailable") from None


@router.get("/api/admin/providers/{provider_id}/models")
async def get_provider_models(provider_id: str,
                              x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    _auth(x_admin_token)
    try:
        return await provider_config.list_provider_models(provider_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid provider selection") from None
    except RuntimeError:
        raise HTTPException(status_code=502, detail="provider model listing unavailable") from None
    except Exception:
        raise HTTPException(status_code=502, detail="provider model listing unavailable") from None


@router.get("/api/providers")
def get_public_providers():
    try:
        return provider_config.list_public_providers()
    except Exception:
        raise HTTPException(status_code=500, detail="provider catalog unavailable") from None


@router.get("/api/providers/{provider_id}/models")
async def get_public_provider_models(provider_id: str):
    try:
        return await provider_config.list_provider_models(provider_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid provider selection") from None
    except RuntimeError:
        raise HTTPException(status_code=502, detail="provider model listing unavailable") from None
    except Exception:
        raise HTTPException(status_code=502, detail="provider model listing unavailable") from None
