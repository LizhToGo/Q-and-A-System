from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from PIL import Image
from sqlalchemy.orm import Session

from .db import get_db
from .models import Material, SourceDocument
from .schemas import EvidenceItem
from .services.ingestion.service import render_page_bytes
from .services.source_evidence import material_evidence, media_definitions, valid_bbox

router = APIRouter(prefix="/api")


def _document(db: Session, document_id: int) -> SourceDocument:
    document = db.get(SourceDocument, document_id)
    if not document or not document.filename.lower().endswith(".pdf") or not Path(document.path).is_file():
        raise HTTPException(status_code=404, detail="教材原始 PDF 不可用")
    return document


def _page_png(document: SourceDocument, page_number: int) -> bytes:
    try:
        return render_page_bytes(document, page_number, dpi=160)
    except (ValueError, IndexError) as exc:
        raise HTTPException(status_code=404, detail="教材页码不存在") from exc


@router.get("/materials/{material_id}/evidence", response_model=EvidenceItem)
def get_material_evidence(material_id: int, db: Session = Depends(get_db)):
    material = db.get(Material, material_id)
    if not material:
        raise HTTPException(status_code=404, detail="教材依据不存在")
    return material_evidence(db, material)


@router.get("/source-documents/{document_id}/pages/{page_number}/image")
def get_page_image(document_id: int, page_number: int, db: Session = Depends(get_db)):
    document = _document(db, document_id)
    return Response(_page_png(document, page_number), media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@router.get("/materials/{material_id}/media/{field}/{index}")
def get_material_media(material_id: int, field: str, index: int, db: Session = Depends(get_db)):
    material = db.get(Material, material_id)
    if not material or field not in {"figures", "tables"} or index < 0:
        raise HTTPException(status_code=404, detail="教材图表不存在")
    media_id = f"{material_id}:{field}:{index}"
    if not any(entry["id"] == media_id and "/media/" in entry["url"] for entry in media_definitions(material)):
        raise HTTPException(status_code=404, detail="教材图表没有经审核的原页位置")
    bbox = material.metadata_json[field][index].get("bbox")
    if not valid_bbox(bbox):
        raise HTTPException(status_code=404, detail="教材图表位置无效")
    document = _document(db, material.source_document_id)
    # Render in display orientation first: PDF rotation must not move the crop to another region.
    with Image.open(BytesIO(_page_png(document, material.page_number))) as image:
        bounds = tuple(round(value * (image.width if axis % 2 == 0 else image.height)) for axis, value in enumerate(bbox))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise HTTPException(status_code=404, detail="教材图表位置无效")
        cropped = image.crop(bounds)
        output = BytesIO()
        rotation = material.metadata_json[field][index].get("display_rotation", 0)
        if isinstance(rotation, bool) or rotation not in {0, 90, 180, 270}:
            raise HTTPException(status_code=404, detail="教材图表显示方向无效")
        if rotation:
            cropped = cropped.rotate(rotation, expand=True)
        cropped.save(output, format="PNG")
    return Response(output.getvalue(), media_type="image/png", headers={"Cache-Control": "private, max-age=300"})
