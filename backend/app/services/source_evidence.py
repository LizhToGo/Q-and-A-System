from __future__ import annotations

import math
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ExtractedItem, IngestionJob, Material, SourceDocument

_NUMBER = r"[A-Za-z]?\d+(?:[ \t]+\d+)*(?:[ \t]*[.．\-－—–][ \t]*\d+(?:[ \t]+\d+)*)*"
_NUMBER_END = r"(?![ \t]*[\d.．\-－—–])"
_REFERENCE = re.compile(r"(图|表)\s*(" + _NUMBER + r")" + _NUMBER_END)
_CAPTION = re.compile(
    r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?((?:图|表)\s*" + _NUMBER + r")" + _NUMBER_END
    + r"(?:[ \t]+(\S[^\n]*))?[ \t]*$"
)


def reference_label(text: str) -> str | None:
    match = _REFERENCE.search(text)
    return match[1] + re.sub(r"[－—–]", "-", re.sub(r"\s+", "", match[2])).replace("．", ".").upper() if match else None


def page_image_url(document_id: int | None, page_number: int | None) -> str | None:
    if not document_id or not page_number:
        return None
    return f"/api/source-documents/{document_id}/pages/{page_number}/image"


def valid_bbox(value: Any) -> bool:
    return (
        isinstance(value, list) and len(value) == 4
        and all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in value)
        and 0 <= value[0] < value[2] <= 1 and 0 <= value[1] < value[3] <= 1
    )


def media_definitions(material: Material, native_text: str = "") -> list[dict[str, Any]]:
    """Only locate media on its actual source page, not metadata describing a reference."""
    if not material.verified or not material.source_document_id or not material.page_number:
        return []
    metadata = material.metadata_json or {}
    if metadata.get("publication_status") == "revoked":
        return []
    captions = {reference_label(match[1]): match[0].strip().lstrip("# ") for match in _CAPTION.finditer(material.content + "\n\n" + native_text)}
    definitions: list[dict[str, Any]] = []
    defined_labels: set[str] = set()
    for field in ("figures", "tables"):
        rows = metadata.get(field)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            label = reference_label(str(row.get("label") or row.get("caption") or ""))
            if row.get("page_number", material.page_number) != material.page_number:
                continue
            bbox = row.get("bbox")
            cropped = valid_bbox(bbox)
            if (not label and not cropped) or (not cropped and label not in captions):
                continue
            media_id = f"{material.id}:{field}:{index}"
            caption = str(row.get("caption") or captions.get(label) or label or ("原页表格（未编号）" if field == "tables" else "原页图形（未编号）"))
            if label and reference_label(caption) != label:
                caption = f"{label} {caption}"
            if label:
                defined_labels.add(label)
            definitions.append({
                "id": media_id, "kind": "table" if field == "tables" or (label and label.startswith("表")) else "figure",
                "label": caption + ("" if cropped else " · 原页（未裁剪）"),
                "page_number": material.page_number,
                "url": f"/api/materials/{material.id}/media/{field}/{index}" if cropped else page_image_url(material.source_document_id, material.page_number),
                "reference": label,
            })
    for label, caption in captions.items():
        if label not in defined_labels:
            definitions.append({
                "id": f"{material.id}:{label}", "kind": "table" if label.startswith("表") else "figure",
                "label": caption + " · 原页（未裁剪）", "page_number": material.page_number,
                "url": page_image_url(material.source_document_id, material.page_number), "reference": label,
            })
    return definitions


def material_evidence(db: Session, material: Material) -> dict[str, Any]:
    metadata = material.metadata_json or {}
    document = db.get(SourceDocument, material.source_document_id) if material.source_document_id else None
    is_pdf = document is not None and document.filename.lower().endswith(".pdf")
    media: list[dict[str, Any]] = []
    if is_pdf:
        references = {reference_label(match[0]) for match in _REFERENCE.finditer(material.content)}
        candidates = db.scalars(select(Material).where(Material.source_document_id == document.id, Material.verified.is_(True)).order_by(Material.page_number, Material.id))
        native_texts = dict(db.execute(select(ExtractedItem.page_number, ExtractedItem.raw_text).join(IngestionJob, IngestionJob.id == ExtractedItem.ingestion_job_id).where(IngestionJob.source_document_id == document.id, ExtractedItem.review_status == "approved").order_by(IngestionJob.id)).all())
        located: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for candidate in candidates:
            for entry in media_definitions(candidate, native_texts.get(candidate.page_number, "")):
                label = entry["reference"]
                if candidate.id != material.id and label not in references:
                    continue
                key = (label or entry["id"], entry["page_number"])
                located.setdefault(key, []).append(entry)
        for entries in located.values():
            crops = [entry for entry in entries if "/media/" in entry["url"]]
            media.extend({key: value for key, value in entry.items() if key != "reference"}
                         for entry in (crops or entries[:1]))
    return {
        "material_id": material.id, "chunk_id": None, "course": material.course.name,
        "topic": material.topic.name if material.topic else None, "title": material.title,
        "material_type": material.material_type, "page_number": material.page_number,
        "section": material.section, "source_anchor": material.source_anchor,
        "snippet": material.content, "score": 0, "verified": material.verified,
        "source_document_id": material.source_document_id,
        "content_format": "markdown" if metadata.get("content_format") == "markdown" else "text",
        "full_content": material.content, "media": media,
        "page_image_url": page_image_url(material.source_document_id, material.page_number) if is_pdf else None,
    }
