from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import UPLOAD_DIR
from ...models import ExtractedItem, IngestionJob, SourceDocument


FORMULA_HINT = re.compile(r"(?:[A-Za-z][A-Za-z0-9_{}]*\s*[=<>≥≤]\s*[^。！？\n]{1,100})")
HEADING_HINT = re.compile(r"^(?:第\s*[一二三四五六七八九十百0-9]+\s*[章节]|[0-9]+(?:\.[0-9]+)*\s+).{2,80}$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.\-\u3400-\u9fff]+")
_OCR_ENGINE: Any | None = None


class OCRUnavailable(RuntimeError):
    """OCR cannot run because its optional CPU runtime is unavailable."""


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _classify(text: str) -> str:
    if FORMULA_HINT.search(text):
        return "formula"
    if "例" in text[:40] or "解：" in text or "解答" in text[:80]:
        return "example"
    if "图" in text[:40] or "波形" in text or "频谱" in text:
        return "waveform"
    return "text"


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Keep model metadata JSON-safe; never evaluate formulas or numeric strings."""
    if depth > 5:
        return None
    if value is None or isinstance(value, (str, bool)):
        return value[:4000] if isinstance(value, str) else value
    if isinstance(value, int):
        return value if abs(value) <= 10**12 else None
    if isinstance(value, float):
        return value if math.isfinite(value) and abs(value) <= 10**12 else None
    if isinstance(value, list):
        return [_safe_json(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, dict):
        return {str(key)[:100]: _safe_json(item, depth=depth + 1) for key, item in list(value.items())[:200]}
    return str(value)[:4000]


def _page_bounds(page: Any) -> list[float]:
    rect = page.rect
    return [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)]


def _text_blocks(page: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    try:
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            lines: list[str] = []
            for line in block.get("lines", []):
                lines.extend(str(span.get("text", "")) for span in line.get("spans", []))
            text = "".join(lines).strip()
            if text:
                blocks.append({"bbox": [float(v) for v in block.get("bbox", [])], "text": text})
    except (AttributeError, TypeError, ValueError):
        return []
    return blocks


def render_page_bytes(source: SourceDocument | Path | str, page_number: int, *, dpi: int = 144) -> bytes:
    """Render one original PDF page to PNG bytes without modifying the source file."""
    import fitz

    path = Path(source.path if isinstance(source, SourceDocument) else source)
    if path.suffix.lower() != ".pdf":
        raise ValueError("只有 PDF 支持页面渲染")
    if page_number < 1:
        raise ValueError("页码必须从 1 开始")
    with fitz.open(path) as document:
        if page_number > len(document):
            raise ValueError("页码超出原始 PDF 范围")
        scale = max(0.5, min(float(dpi) / 72.0, 8.0))
        pixmap = document[page_number - 1].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return pixmap.tobytes("png")


# Descriptive alias for callers exposing a preview endpoint.
render_document_page = render_page_bytes


def _extract_pdf(path: Path) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], str]:
    import fitz

    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    current_section = ""
    with fitz.open(path) as doc:
        for index, page in enumerate(doc, start=1):
            raw_text = page.get_text("text").strip()
            blocks = _text_blocks(page)
            payload = {
                "section": current_section,
                "formulas": FORMULA_HINT.findall(raw_text),
                "images": len(page.get_images(full=True)),
                "source_bounds": _page_bounds(page),
                "page_bounds": _page_bounds(page),
                "text_blocks": blocks,
            }
            if len(raw_text) < 40:
                warnings.append({"page": index, "code": "ocr_required", "message": "页面缺少可复制文本，需要显式 OCR 或模型确认"})
                payload["needs_ocr"] = True
                items.append({
                    "page_number": index, "item_type": "page", "raw_text": raw_text,
                    "normalized_text": raw_text, "payload": payload, "confidence": 0.0,
                    "needs_review": True, "review_status": "pending", "source_anchor": "",
                })
                continue
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            headings = [line for line in lines if HEADING_HINT.match(line)]
            if headings:
                current_section = headings[0]
                payload["section"] = current_section
            formulas = payload["formulas"]
            item_type = _classify(raw_text)
            items.append({
                "page_number": index, "item_type": item_type, "raw_text": raw_text,
                "normalized_text": raw_text, "payload": payload,
                "confidence": 0.72 if len(raw_text) > 200 else 0.55,
                "needs_review": item_type in {"formula", "waveform", "spectrum"} or bool(formulas),
                "review_status": "pending", "source_anchor": "",
            })
        return len(doc), items, warnings, "pymupdf"

def _extract_docx(path: Path) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], str]:
    from docx import Document

    document = Document(path)
    items: list[dict[str, Any]] = []
    for index, paragraph in enumerate(document.paragraphs, start=1):
        text = paragraph.text.strip()
        if not text:
            continue
        item_type = "text"
        if paragraph.style and "Heading" in paragraph.style.name:
            item_type = "heading"
        elif FORMULA_HINT.search(text):
            item_type = "formula"
        items.append({
            "page_number": index, "item_type": item_type, "raw_text": text,
            "normalized_text": text, "payload": {"style": paragraph.style.name if paragraph.style else "", "formulas": FORMULA_HINT.findall(text)},
            "confidence": 0.9, "needs_review": item_type == "formula", "review_status": "pending", "source_anchor": "",
        })
    return len(items), items, [], "python-docx"


def create_ingestion_job(db: Session, source_path: Path, course_id: int | None = None) -> IngestionJob:
    source_path = source_path.resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    digest = sha256_path(source_path)
    existing = db.scalar(select(SourceDocument).where(SourceDocument.file_hash == digest))
    if existing:
        existing_job = db.scalar(select(IngestionJob).where(IngestionJob.source_document_id == existing.id).order_by(IngestionJob.id.desc()))
        if existing_job:
            return existing_job
    extension = source_path.suffix.lower()
    if extension == ".pdf":
        page_count, items, warnings, parser = _extract_pdf(source_path)
    elif extension == ".docx":
        page_count, items, warnings, parser = _extract_docx(source_path)
    elif extension in {".md", ".markdown", ".txt"}:
        content = source_path.read_text(encoding="utf-8", errors="replace")
        page_count, items, warnings, parser = 1, [{
            "page_number": 1, "item_type": "text", "raw_text": content, "normalized_text": content,
            "payload": {"source_bounds": [0.0, 0.0, 1.0, 1.0]}, "confidence": 0.95,
            "needs_review": True, "review_status": "pending", "source_anchor": "",
        }], [], "text"
    else:
        raise ValueError("仅支持 PDF、DOCX、Markdown 和 TXT")

    document = SourceDocument(course_id=course_id, filename=source_path.name, file_hash=digest, path=str(source_path), page_count=page_count, parser=parser, status="review", warnings=warnings)
    db.add(document)
    db.flush()
    job = IngestionJob(source_document_id=document.id, status="review", processed_pages=page_count, total_pages=page_count,
                       summary={"warnings": len(warnings), "items": len(items), "ocr_pages": sum(1 for item in items if item["payload"].get("needs_ocr"))})
    db.add(job)
    db.flush()
    for item in items:
        # The database document id, not a model-generated string, is authoritative.
        item["source_anchor"] = f"doc:{document.id}:p{item['page_number']}"
        db.add(ExtractedItem(ingestion_job_id=job.id, **item))
    db.commit()
    db.refresh(job)
    return job


def _safe_basename(filename: str) -> str:
    name = Path(filename or "upload.bin").name
    name = _SAFE_NAME.sub("_", name).strip(" .") or "upload.bin"
    return name[:180]


def save_upload(filename: str, content: bytes) -> Path:
    """Store immutable uploads under content hash plus a sanitized basename."""
    digest = hashlib.sha256(content).hexdigest()
    path = UPLOAD_DIR / f"{digest}-{_safe_basename(filename)}"
    if not path.exists():
        path.write_bytes(content)
    return path


def list_job_items(db: Session, job_id: int) -> list[ExtractedItem]:
    return list(db.scalars(select(ExtractedItem).where(ExtractedItem.ingestion_job_id == job_id).order_by(ExtractedItem.page_number, ExtractedItem.id)))


def _chunk_text(text: str, max_chars: int = 1000, overlap_paragraphs: int = 1) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text.strip()] if text.strip() else []
    chunks: list[str] = []
    current: list[str] = []
    for paragraph in paragraphs:
        pieces = [paragraph[i:i + max_chars] for i in range(0, len(paragraph), max_chars)] or [""]
        for piece in pieces:
            candidate = "\n\n".join(current + [piece])
            if current and len(candidate) > max_chars:
                chunks.append("\n\n".join(current)[:max_chars])
                current = current[-overlap_paragraphs:] if len(current[-1]) <= max_chars else []
                while current and len("\n\n".join(current + [piece])) > max_chars:
                    current = current[1:]
            current.append(piece)
    if current:
        chunks.append("\n\n".join(current)[:max_chars])
    return [chunk for chunk in chunks if chunk]


def _valid_bounds(payload: dict[str, Any]) -> bool:
    bounds = payload.get("source_bounds") or payload.get("page_bounds")
    return isinstance(bounds, list) and len(bounds) == 4 and all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in bounds)


def publish_job(db: Session, job_id: int, course_id: int) -> dict[str, int]:
    from ...models import Formula, Material, MaterialChunk, Topic

    job = db.get(IngestionJob, job_id)
    if not job:
        raise LookupError("导入任务不存在")
    document = db.get(SourceDocument, job.source_document_id)
    if not document:
        raise LookupError("导入任务缺少原始教材")
    items = list_job_items(db, job_id)
    topic_cache = {topic.name: topic for topic in db.scalars(select(Topic).where(Topic.course_id == course_id))}
    published = 0
    skipped = 0
    for item in items:
        payload = _safe_json(item.payload or {})
        text = (item.normalized_text or "").strip()
        risky = item.item_type in {"formula", "waveform", "spectrum"} or bool(payload.get("needs_ocr") or payload.get("ocr_engine"))
        if item.review_status != "approved" or not text or (risky and not _valid_bounds(payload)):
            skipped += 1
            continue
        source_anchor = f"doc:{document.id}:p{item.page_number}"
        duplicate = db.scalar(select(Material).where(Material.source_document_id == document.id, Material.page_number == item.page_number, Material.source_anchor == source_anchor))
        if duplicate:
            skipped += 1
            continue
        section = str(payload.get("topic") or payload.get("section") or "").strip()[:200]
        topic = topic_cache.get(section)
        if section and topic is None:
            topic = Topic(course_id=course_id, name=section, description="教材审核后导入")
            db.add(topic)
            db.flush()
            topic_cache[section] = topic
        material_type = item.item_type if item.item_type in {"text", "formula", "example", "waveform", "spectrum"} else "text"
        material = Material(course_id=course_id, topic_id=topic.id if topic else None, source_document_id=document.id,
                            material_type=material_type, title=section or f"导入资料第{item.page_number}页", content=text,
                            page_number=item.page_number, section=section, source_anchor=source_anchor,
                            tags=[section] if section else [], metadata_json=payload, verified=True)
        db.add(material)
        db.flush()
        for index, chunk in enumerate(_chunk_text(text)):
            db.add(MaterialChunk(material_id=material.id, chunk_index=index, text=chunk, keywords=[section] if section else []))
        formulas = payload.get("formulas", [])
        if isinstance(formulas, list):
            for formula_text in formulas:
                if isinstance(formula_text, str) and formula_text.strip():
                    raw_formula = formula_text.strip()[:4000]
                    db.add(Formula(material_id=material.id, raw_formula=raw_formula, canonical_expression=raw_formula, variables_json={}))
        published += 1
    job.status = "published"
    job.summary = {**(job.summary or {}), "published": published, "skipped": skipped}
    db.commit()
    return {"published": published, "skipped": skipped}


def _load_ocr_engine() -> Any:
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise OCRUnavailable("未安装 rapidocr_onnxruntime；请安装 OCR 依赖后再运行显式 OCR") from exc
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _run_ocr(image_bytes: bytes) -> tuple[str, list[dict[str, Any]], float]:
    try:
        import cv2
        import numpy as np
        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        result_value = _load_ocr_engine()(image)
        result = result_value[0] if isinstance(result_value, tuple) and len(result_value) == 2 else result_value
    except OCRUnavailable:
        raise
    except Exception as exc:
        raise OCRUnavailable(f"OCR 执行失败：{exc.__class__.__name__}") from exc
    blocks: list[dict[str, Any]] = []
    for row in result or []:
        if isinstance(row, dict):
            box = row.get("box") or row.get("bbox") or []
            text = str(row.get("text") or "").strip()
            score = float(row.get("score", row.get("confidence", 0.0)) or 0.0)
        else:
            try:
                box, text, score = row[0], str(row[1]).strip(), float(row[2])
            except (IndexError, TypeError, ValueError):
                continue
        if text:
            if hasattr(box, "tolist"):
                box = box.tolist()
            blocks.append({"bbox": _safe_json(box), "text": text[:4000], "confidence": max(0.0, min(1.0, score))})

    return "\n".join(block["text"] for block in blocks), blocks, (sum(block["confidence"] for block in blocks) / len(blocks) if blocks else 0.0)

def ocr_page(source: SourceDocument | Path | str, page_number: int) -> dict[str, Any]:
    """OCR one page on demand; native ingestion never invokes this implicitly."""
    image = render_page_bytes(source, page_number)
    text, blocks, confidence = _run_ocr(image)
    bounds = _safe_json(_page_bounds_for_source(source, page_number))
    return {"page_number": page_number, "raw_text": text, "normalized_text": text, "blocks": blocks, "boxes": blocks,
            "confidence": confidence, "source_bounds": bounds, "page_bounds": bounds,
            "warning": "OCR 结果必须人工审核" if text else "OCR 未识别到文本"}


def _page_bounds_for_source(source: SourceDocument | Path | str, page_number: int) -> list[float]:
    import fitz
    path = Path(source.path if isinstance(source, SourceDocument) else source)
    with fitz.open(path) as document:
        if page_number < 1 or page_number > len(document):
            raise ValueError("页码超出原始 PDF 范围")
        return _page_bounds(document[page_number - 1])


def ocr_item(db: Session, item_id: int) -> ExtractedItem:
    item = db.get(ExtractedItem, item_id)
    if not item:
        raise LookupError("解析项不存在")
    job = db.get(IngestionJob, item.ingestion_job_id)
    document = db.get(SourceDocument, job.source_document_id) if job else None
    if not document:
        raise LookupError("解析项缺少原始教材")
    result = ocr_page(document, item.page_number)
    payload = dict(item.payload or {})
    payload.update({"ocr_engine": "rapidocr_onnxruntime", "ocr_blocks": result["blocks"], "ocr_boxes": result["boxes"], "source_bounds": result["source_bounds"], "page_bounds": result["page_bounds"], "needs_ocr": False, "ocr_warning": result["warning"]})
    item.raw_text = result["raw_text"]
    item.normalized_text = result["normalized_text"]
    item.payload = _safe_json(payload)
    item.item_type = _classify(item.normalized_text) if item.normalized_text else "page"
    item.confidence = result["confidence"]
    item.needs_review = True
    item.review_status = "pending"
    db.commit()
    db.refresh(item)
    return item


# Explicit aliases make the on-demand API discoverable without changing the route layer.
ocr_extracted_item = ocr_item


def ocr_job_pages(db: Session, job_id: int, pages: list[int] | tuple[int, ...]) -> list[ExtractedItem]:
    """OCR only the requested pages; no background/eager whole-book OCR is performed."""
    requested = sorted({int(page) for page in pages})
    if not requested:
        return []
    items = {item.page_number: item for item in list_job_items(db, job_id)}
    output: list[ExtractedItem] = []
    for page in requested:
        if page not in items:
            raise ValueError(f"任务中不存在第 {page} 页")
        output.append(ocr_item(db, items[page].id))
    return output


ocr_selected_pages = ocr_job_pages
