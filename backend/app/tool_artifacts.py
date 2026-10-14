"""Serve only completed, opaque-ID PNG artifacts from the local tool directory."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response

from .services import local_tools

router = APIRouter(prefix="/api")
_ARTIFACT_ID = re.compile(r"[0-9a-f]{32}\Z")


def get_artifact_directory() -> Path:
    """Dependency override permits isolated media tests without touching real data."""
    return local_tools.ARTIFACT_DIR


@router.get("/tool-artifacts/{artifact_id}")
def get_tool_artifact(artifact_id: str, directory: Path = Depends(get_artifact_directory)):
    if _ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise HTTPException(status_code=404, detail="Local tool artifact not found")
    root = Path(directory).resolve()
    path = root / f"{artifact_id}.png"
    try:
        if path.is_symlink() or path.resolve().parent != root or not path.is_file():
            raise HTTPException(status_code=404, detail="Local tool artifact not found")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise HTTPException(status_code=404, detail="Local tool artifact not found")
        image = path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Local tool artifact not found") from exc
    if not image.startswith(local_tools.PNG_SIGNATURE):
        raise HTTPException(status_code=404, detail="Local tool artifact not found")
    return Response(image, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})
