from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select, update

from ...db import SessionLocal
from ...models import EvidenceLink, ExtractedItem, Formula, IngestionJob, Material, MaterialChunk, SourceDocument
from ...provider_config import ProviderConfig, get_provider_config
from ..source_evidence import valid_bbox
from .page_transcription import PDF_RENDER_LOCK
from .assist import AssistUnavailable, UnsupportedImages
from .service import _chunk_text, _pdf_quality, ocr_page, sha256_path

# Change whenever input selection, transcription validation or checkpoint semantics change.
IMPORT_SCHEMA_VERSION = 3
_OCR_LOCK = threading.Lock()
_VISION_UNAVAILABLE: set[tuple[str, str]] = set()
_ALLOWED_TYPES = {"text", "formula", "example", "waveform", "spectrum", "page"}
_MODEL_FIELDS = {"section", "chapter", "content_format", "reviewed_formulas", "formulas", "figures", "tables", "printed_page", "uncertainties"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".pending-{uuid.uuid4().hex[:16]}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def provider_identity(config: ProviderConfig | None) -> dict[str, Any]:
    from importlib.metadata import version
    # Local checkpoints never load credentials or pretend to have called a remote provider.
    if config is None:
        return {"id": "local-ocr", "config_source": "local", "provider": "local_ocr",
                "base_url": "", "model": "rapidocr", "gateway_messages": False,
                "local_schema_version": 2, "engine_version": version("rapidocr")}
    # Credentials are intentionally neither accessed nor hashed nor serialized.
    return {"id": config.id, "config_source": config.config_source, "provider": config.provider,
            "base_url": config.base_url, "model": config.model, "gateway_messages": config.gateway_messages}


def _strip_provenance(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_provenance(child) for key, child in value.items()
                if key not in {"source_anchor", "source_bounds", "page_bounds", "source_document_id", "source_page", "file_hash", "source_text_sha256", "verified", "approved", "review_status", "quality_review_cleared", "publication_status"}}
    if isinstance(value, list):
        return [_strip_provenance(child) for child in value]
    return value


def validate_result(value: Any, page: int, *, local: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or isinstance(value.get("source_page"), bool) or value.get("source_page") != page:
        raise ValueError("转写来源页不匹配")
    text = value.get("normalized_text")
    blank = local and value.get("item_type") == "blank"
    if not isinstance(text, str) or (not text.strip() and not blank):
        raise ValueError("转写正文为空")
    if value.get("item_type") not in _ALLOWED_TYPES and not blank:
        raise ValueError("转写类型不支持")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("转写元数据不是对象")
    clean = _strip_provenance({key: child for key, child in payload.items() if key in _MODEL_FIELDS})
    clean["content_format"] = "text" if local else "markdown"
    if local:
        clean["reviewed_formulas"] = []
        clean["formulas"] = []
        for field in ("blank_evidence", "paragraphs", "blank_notice"):
            if field in payload:
                clean[field] = payload[field]
        if blank and (text or not isinstance(clean.get("blank_evidence"), dict)
                      or clean["blank_evidence"].get("is_blank") is not True):
            raise ValueError("空白页缺少原页空白检测证据")
    for field in ("figures", "tables"):
        rows = clean.get(field, [])
        if not isinstance(rows, list):
            raise ValueError(f"{field} 必须为数组")
        safe_rows = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{field} 项必须为对象")
            row = dict(row)
            row["page_number"] = page
            box = row.get("bbox")
            if box is not None and not valid_bbox(box):
                row.pop("bbox", None)
            safe_rows.append(row)
        clean[field] = safe_rows
    for field in ("formulas", "reviewed_formulas", "uncertainties"):
        if not isinstance(clean.get(field, []), list):
            raise ValueError(f"{field} 必须为数组")
    confidence = value.get("confidence", 0.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("转写置信度无效")
    warnings = value.get("warnings", [])
    if not isinstance(warnings, list):
        raise ValueError("转写警告必须为数组")
    result = {"source_page": page, "normalized_text": text, "item_type": value["item_type"],
              "payload": clean, "confidence": confidence, "warnings": warnings,
              "diagnostics": value.get("diagnostics", {})}
    _json(result)  # Reject NaN and non-JSON results rather than truncating full text.
    return result


def _retry_details(error: Exception) -> tuple[bool, float | None]:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        status = getattr(current, "status_code", None) or getattr(response, "status_code", None)
        if status in {408, 429, 500, 502, 503, 504}:
            raw = getattr(current, "retry_after", None)
            if raw is None and response is not None:
                raw = response.headers.get("Retry-After")
            try:
                delay = max(0.0, float(raw))
                if not math.isfinite(delay):
                    raise ValueError("Retry-After 非有限值")
            except (ValueError, TypeError):
                try:
                    parsed = parsedate_to_datetime(str(raw))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    delay = max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    delay = None
            return True, delay
        # Explicit transport failures only; semantic/empty/JSON errors are not retryable.
        if current.__class__.__name__ in {"ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectError", "ReadError"}:
            return True, None
        current = current.__cause__ or current.__context__
    return False, None


def _transcribe(document: SourceDocument, page: int, native: str, ocr: str, config: ProviderConfig,
                mode: str, retries: int, max_output_tokens: int) -> dict[str, Any]:
    from .page_transcription import transcribe_page

    for attempt in range(retries + 1):
        try:
            return validate_result(transcribe_page(document, page, native_text=native, ocr_text=ocr,
                                   mode=mode, config=config, max_output_tokens=max_output_tokens), page)
        except Exception as error:
            retryable, delay = _retry_details(error)
            if attempt >= retries or not retryable:
                raise
            time.sleep(delay if delay is not None else min(60.0, 2.0 ** (attempt + 1)))
    raise RuntimeError("不可达的重试状态")


def _load_checkpoint(path: Path, identity: dict[str, Any]) -> dict[str, Any] | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("status") != "success" or record.get("identity") != identity:
            return None
        result = validate_result(record["result"], identity["page_number"], local=identity["mode"] == "ocr")
        if _digest(_json(result)) != record.get("result_sha256"):
            return None
        ocr = record["ocr"]
        if not isinstance(ocr, dict) or not isinstance(ocr.get("text"), str) or not isinstance(ocr.get("blocks"), list):
            return None
        if _digest(_json(ocr)) != record.get("ocr_sha256"):
            return None
        return record
    except (OSError, ValueError, TypeError, KeyError):
        return None


class LocalOCRFailure(ValueError):
    def __init__(self, reason: str, blank_evidence: dict[str, Any] | None = None):
        super().__init__(reason)
        self.blank_evidence = blank_evidence


def _blank_evidence(document: SourceDocument, page_number: int) -> dict[str, Any]:
    """Only a near-white *entire* rendered source can explain empty OCR.

    No border cropping or noise removal: covers, faint scans and grey margins
    are deliberately not certified blank. Annotations are included in the render.
    """
    import fitz
    import numpy as np

    with PDF_RENDER_LOCK, fitz.open(document.path) as pdf:
        page = pdf[page_number - 1]
        native_chars = len(page.get_text("text").strip())
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), colorspace=fitz.csRGB, alpha=False, annots=True)
        samples = pixmap.samples
        pixels = np.frombuffer(samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
        nonwhite = int(np.count_nonzero(np.any(pixels < 254, axis=2)))
        minimum = int(pixels.min())
        return {"method": "full_page_rgb_144dpi_no_crop", "threshold": 254,
                "width": pixmap.width, "height": pixmap.height, "pixel_count": pixmap.width * pixmap.height,
                "nonwhite_pixels": nonwhite, "minimum_channel": minimum, "native_text_characters": native_chars,
                "raster_sha256": hashlib.sha256(samples).hexdigest(),
                "is_blank": native_chars == 0 and nonwhite == 0}


def _ocr_paragraphs(ocr: dict[str, Any]) -> list[str]:
    """Whitespace layout segmentation; preserve every recognized block and box."""
    rows = []
    for index, block in enumerate(ocr["blocks"]):
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            raise LocalOCRFailure("本地 OCR 框数据无效，不能伪造阅读顺序")
        box = block.get("bbox")
        if not isinstance(box, list) or len(box) < 4 or any(
                not isinstance(point, list) or len(point) != 2 or any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in point) for point in box):
            raise LocalOCRFailure("本地 OCR 缺少真实有效框，不能伪造阅读顺序")
        xs, ys = [point[0] for point in box], [point[1] for point in box]
        rect = (min(xs), min(ys), max(xs), max(ys))
        if rect[0] >= rect[2] or rect[1] >= rect[3]:
            raise LocalOCRFailure("本地 OCR 框面积无效")
        rows.append((rect, index, block))
    if not rows:
        raise LocalOCRFailure("本地 OCR 有文字却没有真实识别框")

    def segments(group, depth=0):
        heights = sorted(row[0][3] - row[0][1] for row in group)
        height = heights[len(heights) // 2]
        if len(group) > 1 and depth < 32:
            # A column gutter first, then paragraph whitespace. Intervals must
            # be truly disjoint, so spanning headings prevent an invalid split.
            for axis, minimum_gap in ((0, 2 * height), (1, 0.75 * height)):
                ordered = sorted(group, key=lambda row: row[0][axis])
                end = ordered[0][0][axis + 2]
                cuts = []
                for i, row in enumerate(ordered[1:], 1):
                    gap = row[0][axis] - end
                    if gap >= minimum_gap:
                        cuts.append((gap, i))
                    end = max(end, row[0][axis + 2])
                if cuts:
                    _, cut = max(cuts)
                    return segments(ordered[:cut], depth + 1) + segments(ordered[cut:], depth + 1)
        return [group]

    paragraphs = []
    ordered_blocks = []
    for group in segments(rows):
        lines = []
        for row in sorted(group, key=lambda row: (row[0][1], row[0][0], row[1])):
            # Join horizontal fragments only when their vertical centres agree.
            centre = (row[0][1] + row[0][3]) / 2
            height = row[0][3] - row[0][1]
            if lines and abs(centre - lines[-1][0]) <= min(height, lines[-1][1]) * 0.4:
                lines[-1][2].append(row)
            else:
                lines.append([centre, height, [row]])
        text_lines = []
        for _, _, line in lines:
            line.sort(key=lambda row: (row[0][0], row[1]))
            text_lines.append(" ".join(row[2]["text"] for row in line))
            for _, _, block in line:
                ordered_blocks.append({**block, "reading_order": len(ordered_blocks), "paragraph_index": len(paragraphs)})
        paragraphs.append("\n".join(text_lines))
    ocr["blocks"] = ordered_blocks
    return paragraphs


def _local_result(document: SourceDocument, page_number: int, native: str,
                  ocr: dict[str, Any], scanned: bool) -> dict[str, Any]:
    diagnostics = {"mode": "ocr", "protocol": "local_ocr", "human_reviewed": False,
                   "remote_provider_called": False}
    payload: dict[str, Any] = {"content_format": "text", "reviewed_formulas": [], "formulas": [],
                               "uncertainties": ["本地解析/OCR 未验证公式；损坏或不确定文字须对照原页人工审核"]}
    if not ocr["text"].strip():
        evidence = _blank_evidence(document, page_number)
        if not evidence["is_blank"]:
            raise LocalOCRFailure("实际 OCR 为空，原页未通过严格空白检测；扫描图、封面、灰边及噪点不能冒充空白页", evidence)
        payload.update({"blank_evidence": evidence, "blank_notice": "原页空白，无可检索正文；仍待人工审查",
                        "paragraphs": []})
        diagnostics["text_source"] = "verified_blank_source"
        text, item_type, confidence = "", "blank", 0.0
    else:
        paragraphs = _ocr_paragraphs(ocr)
        if native.strip() and not scanned:
            text = native
            payload["paragraphs"] = native.split("\n\n")
            diagnostics["text_source"] = "native_pdf"
        else:
            text = "\n\n".join(paragraphs)
            payload["paragraphs"] = paragraphs
            diagnostics["text_source"] = "ocr_blocks"
        item_type, confidence = "text", ocr["confidence"]
    return validate_result({"source_page": page_number, "normalized_text": text, "item_type": item_type,
                            "payload": payload, "confidence": confidence,
                            "warnings": ["本地解析/OCR 结果及公式均待人工审核"], "diagnostics": diagnostics},
                           page_number, local=True)


def _prepare_page(document_data: dict[str, Any], item_data: dict[str, Any], *, output_dir: Path,
                  config: ProviderConfig | None, mode: str, force: bool, retries: int, max_output_tokens: int) -> dict[str, Any]:
    import fitz

    document = SourceDocument(**document_data)
    page_number = item_data["page_number"]
    with PDF_RENDER_LOCK, fitz.open(document.path) as pdf:
        page = pdf[page_number - 1]
        native = page.get_text("text")
        bounds = [float(page.rect.x0), float(page.rect.y0), float(page.rect.x1), float(page.rect.y1)]
        quality = _pdf_quality(page, native)
        native_ordered = "\n\n".join(block[4].strip() for block in page.get_text("blocks", sort=True)
                                    if block[6] == 0 and block[4].strip()) if mode == "ocr" else native
    identity = {"schema_version": IMPORT_SCHEMA_VERSION, "document_id": document.id,
                "file_hash": document.file_hash, "page_number": page_number,
                "raw_text_sha256": _digest(item_data["raw_text"]), "native_text_sha256": _digest(native),
                "bounds": bounds, "quality": quality, "provider": provider_identity(config),
                "mode": mode, "max_output_tokens": None if mode == "ocr" else max_output_tokens}
    version = _digest(_json(identity))
    directory = output_dir / "checkpoints" / document.file_hash / f"p{page_number:04d}"
    checkpoint_path = directory / f"{version[:32]}.json"
    pointer_path = directory / f"{version[:32]}.latest.json"
    if not force and pointer_path.exists():
        try:
            latest = json.loads(pointer_path.read_text(encoding="utf-8"))["checkpoint"]
            if isinstance(latest, str) and Path(latest).name == latest:
                checkpoint_path = directory / latest
        except (OSError, ValueError, TypeError, KeyError):
            pass
    record = None if force else _load_checkpoint(checkpoint_path, identity)
    resumed = record is not None
    if record is None:
        ocr = {"text": "", "blocks": [], "confidence": 0.0}
        provider_key = (config.base_url, config.model) if config is not None else None
        scanned = bool(quality["quality_flags"] or not native.strip())
        actual_mode = "vision" if mode == "auto" and scanned and provider_key not in _VISION_UNAVAILABLE else mode
        try:
            def read_ocr() -> dict[str, Any]:
                with _OCR_LOCK, PDF_RENDER_LOCK:
                    extracted = ocr_page(document, page_number)
                return {"text": extracted["raw_text"], "blocks": extracted["blocks"], "confidence": extracted["confidence"]}
            if mode == "ocr":
                ocr = read_ocr()
                result = _local_result(document, page_number, native_ordered, ocr, scanned)
            else:
                if actual_mode != "vision" and scanned:
                    ocr = read_ocr()
                    if not ocr["text"].strip() and mode == "text":
                        raise ValueError("实际 OCR 为空，不能进行文本模式转写")
                try:
                    result = _transcribe(document, page_number, native, ocr["text"], config, actual_mode, retries, max_output_tokens)
                except UnsupportedImages as error:
                    if mode != "auto":
                        raise
                    if getattr(error, "status_code", None) in {400, 415, 422}:
                        _VISION_UNAVAILABLE.add(provider_key)
                    ocr = read_ocr()
                    if not ocr["text"].strip():
                        raise ValueError("视觉不可用且实际 OCR 为空，不能伪造全文") from error
                    result = _transcribe(document, page_number, native, ocr["text"], config, "text", retries, max_output_tokens)
            record = {"status": "success", "identity": identity, "version": version, "result": result,
                      "result_sha256": _digest(_json(result)), "ocr": ocr, "ocr_sha256": _digest(_json(ocr)),
                      "created_at": datetime.now(timezone.utc).isoformat()}
            # Forced generations get their own immutable version, including identical outputs.
            if force:
                checkpoint_path = directory / f"{version[:32]}-{uuid.uuid4().hex[:16]}.json"
            _atomic_json(checkpoint_path, record)
            _atomic_json(pointer_path, {"checkpoint": checkpoint_path.name})
        except Exception as error:
            _atomic_json(directory / f"{version[:32]}.error.json", {"status": "failed", "identity": identity,
                         "error_type": error.__class__.__name__, "page_proposal": getattr(error, "page_proposal", None),
                         "reason": str(error) if isinstance(error, (AssistUnavailable, LocalOCRFailure)) else "本地来源、检查点或数据库处理失败",
                         "upstream": getattr(error, "upstream_diagnostics", {}),
                         "blank_evidence": getattr(error, "blank_evidence", None),
                         "created_at": datetime.now(timezone.utc).isoformat()})
            raise
    return {**record, "resumed": resumed, "checkpoint_path": str(checkpoint_path.resolve())}


def _state_hash(item: ExtractedItem) -> str:
    return _digest(_json({"text": item.normalized_text or "", "item_type": item.item_type,
                          "payload": {key: value for key, value in (item.payload or {}).items() if key != "library_import"},
                          "confidence": item.confidence, "review_status": item.review_status,
                          "needs_review": item.needs_review}))


def _protected(item: ExtractedItem, material: Material | None) -> bool:
    if item.review_status != "pending" or (material is not None and (
            material.verified or material.content.strip() != (item.normalized_text or "").strip())):
        return True
    marker = (item.payload or {}).get("library_import", {})
    if marker.get("active_state_sha256"):
        return _state_hash(item) != marker["active_state_sha256"]
    return bool((item.normalized_text or "").strip() != (item.raw_text or "").strip()
                or (item.payload or {}).get("quality_review_cleared") is True)


def _candidate_index(db: Any, document: SourceDocument, item: ExtractedItem, material: Material | None) -> None:
    if document.course_id is None:
        raise ValueError("教材未关联课程")
    if material is None:
        material = Material(source_document_id=document.id, source_anchor=item.source_anchor)
        db.add(material)
    material.course_id = document.course_id
    material.page_number = item.page_number
    material.material_type = item.item_type if item.item_type not in {"page", "blank"} else "text"
    material.section = str((item.payload or {}).get("section") or "")[:300]
    material.title = f"第 {item.page_number} 页" + (f" · {material.section}" if material.section else "")
    material.content = item.normalized_text
    material.tags = [document.filename] + ([material.section] if material.section else [])
    material.metadata_json = {**(material.metadata_json or {}), **item.payload,
                              "source_document_id": document.id, "source_page": item.page_number,
                              "publication_status": "indexed_unverified",
                              "index_quality": "blank" if item.item_type == "blank" else "candidate",
                              "rag_indexed": item.item_type != "blank", "rag_index_version": 2}
    material.verified = False
    db.flush()
    old_chunks = {chunk.chunk_index: chunk for chunk in material.chunks}
    for index, text in enumerate(_chunk_text(item.normalized_text) if item.item_type != "blank" else []):
        chunk = old_chunks.pop(index, None)
        if chunk is None:
            db.add(MaterialChunk(material_id=material.id, chunk_index=index, text=text, keywords=material.tags))
        else:
            chunk.text = text
            chunk.keywords = material.tags
    for chunk in old_chunks.values():
        db.execute(update(EvidenceLink).where(EvidenceLink.chunk_id == chunk.id).values(chunk_id=None))
        db.delete(chunk)
    for formula in list(material.formulas):
        db.delete(formula)
    for formula in dict.fromkeys(value for value in (item.payload or {}).get("formulas", []) if isinstance(value, str) and value.strip()):
        db.add(Formula(material_id=material.id, raw_formula=formula, canonical_expression=formula, variables_json={}))


def _review_snapshot(item: ExtractedItem) -> dict[str, Any]:
    payload = dict(item.payload or {})
    return {"item_id": item.id, "page_number": item.page_number, "source_anchor": item.source_anchor,
            "item_type": item.item_type, "raw_text": item.raw_text or "", "normalized_text": item.normalized_text or "",
            "source_text_sha256": _digest(item.raw_text or ""), "payload": payload,
            "quality_flags": payload.get("quality_flags", []), "quality_review_cleared": payload.get("quality_review_cleared") is True,
            "confidence": item.confidence, "review_status": item.review_status,
            "approved": False, "approval_method": "pending", "import_status": "not_processed"}


def _apply_page(document_id: int, item_id: int, record: dict[str, Any], session_factory: Callable[..., Any]) -> dict[str, Any]:
    with session_factory() as db:
        document = db.get(SourceDocument, document_id)
        item = db.get(ExtractedItem, item_id)
        if document is None or item is None or _digest(item.raw_text or "") != record["identity"]["raw_text_sha256"]:
            raise ValueError("页面原始文本或来源已变化")
        if item.page_number != record["identity"]["page_number"] or document.file_hash != record["identity"]["file_hash"]:
            raise ValueError("页面来源身份已变化")
        material = db.scalar(select(Material).where(Material.source_document_id == document_id,
                            Material.page_number == item.page_number, Material.source_anchor == f"doc:{document_id}:p{item.page_number}"))
        protected = _protected(item, material)
        result = record["result"]
        marker = dict((item.payload or {}).get("library_import", {}))
        if (not protected and marker.get("active_checkpoint") == record["checkpoint_path"]
                and marker.get("active_result_sha256") == record["result_sha256"]):
            snapshot = _review_snapshot(item)
            snapshot["import_status"] = "resumed" if record["resumed"] else "success"
            return snapshot
        version_info = {"checkpoint": record["checkpoint_path"], "version": record["version"],
                        "result_sha256": record["result_sha256"], "model": record["identity"]["provider"]["model"]}
        if protected:
            # Proposals live in immutable checkpoints; even metadata on reviewed rows stays untouched.
            snapshot = _review_snapshot(item)
            snapshot.update({"import_status": "preserved_reviewed", "proposed_version": version_info})
            return snapshot
        payload = dict(item.payload or {})
        for key in _MODEL_FIELDS:
            payload.pop(key, None)
        for key in ("blank_evidence", "blank_notice", "paragraphs"):
            payload.pop(key, None)
        payload.update(result["payload"])
        identity = record["identity"]
        payload.update({"source_anchor": f"doc:{document_id}:p{item.page_number}", "source_document_id": document_id,
                        "source_page": item.page_number, "file_hash": document.file_hash,
                        "source_bounds": identity["bounds"], "page_bounds": identity["bounds"],
                        "source_text_sha256": identity["raw_text_sha256"], "native_text_sha256": identity["native_text_sha256"],
                        "ocr_text": record["ocr"]["text"], "ocr_blocks": record["ocr"]["blocks"],
                        "ocr_boxes": record["ocr"]["blocks"], "ocr_confidence": record["ocr"]["confidence"],
                        "needs_ocr": False, "transcription_warnings": result["warnings"],
                        "transcription_diagnostics": result["diagnostics"], "quality_review_cleared": False,
                        "text_quality": "needs_review"})
        local = identity["mode"] == "ocr"
        for key in ("llm_provider", "llm_model"):
            payload.pop(key, None)
        if not local:
            payload.update({"llm_provider": identity["provider"]["provider"], "llm_model": identity["provider"]["model"]})
        flags = [flag for flag in payload.get("quality_flags", [])
                 if flag not in {"model_transcription_requires_review", "local_ocr_requires_review"}]
        payload["quality_flags"] = list(dict.fromkeys(flags + identity["quality"]["quality_flags"]
                                                    + ["local_ocr_requires_review" if local else "model_transcription_requires_review"]))
        if record["ocr"]["text"]:
            payload["ocr_engine"] = "rapidocr"
        versions = list(marker.get("versions", []))
        if version_info not in versions:
            versions.append(version_info)
        marker.update({"versions": versions, "active_checkpoint": record["checkpoint_path"],
                       "active_result_sha256": record["result_sha256"]})
        payload["library_import"] = marker
        item.payload = payload
        item.normalized_text = result["normalized_text"]
        item.item_type = result["item_type"]
        item.confidence = result["confidence"]
        item.source_anchor = f"doc:{document_id}:p{item.page_number}"
        item.review_status = "pending"
        item.needs_review = True
        marker["active_state_sha256"] = _state_hash(item)
        item.payload = {**payload, "library_import": marker}
        _candidate_index(db, document, item, material)
        job = db.get(IngestionJob, item.ingestion_job_id)
        job.status = "review"
        document.status = "review"
        db.commit()  # One page, one transaction; no network or OCR inside the transaction.
        snapshot = _review_snapshot(item)
        snapshot["import_status"] = "resumed" if record["resumed"] else "success"
        return snapshot


def load_document_specs(manifest: Path | None, document_ids: list[int] | None) -> list[dict[str, Any]]:
    if manifest is None:
        specs = [{"document_id": value} for value in (document_ids or [])]
    else:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("documents"), list):
            raise ValueError("书清单必须包含 schema_version=1 和 documents 数组")
        specs = value["documents"]
    if not specs or any(not isinstance(spec, dict) or isinstance(spec.get("document_id"), bool)
                        or not isinstance(spec.get("document_id"), int) or spec["document_id"] < 1 for spec in specs):
        raise ValueError("必须提供正整数 document_id")
    if len({spec["document_id"] for spec in specs}) != len(specs):
        raise ValueError("document_id 重复")
    return specs


def import_library(specs: list[dict[str, Any]], *, output_dir: Path, pages: list[int] | None = None,
                   provider_id: str | None = None, model: str | None = None, mode: str = "auto", workers: int = 2,
                   retries: int = 2, force: bool = False, max_output_tokens: int = 8192,
                   session_factory: Callable[..., Any] = SessionLocal,
                   progress: Callable[[str], None] = print) -> dict[str, Any]:
    if mode not in {"auto", "text", "vision", "ocr"} or not 1 <= workers <= 8 or not 0 <= retries <= 5 or not 1024 <= max_output_tokens <= 16384:
        raise ValueError("mode/workers/retries/max_output_tokens 超出支持范围")
    if pages is not None and (not pages or any(isinstance(p, bool) or not isinstance(p, int) or p < 1 for p in pages)):
        raise ValueError("pages 必须为非空正整数数组")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = None if mode == "ocr" else get_provider_config(provider_id=provider_id, model=model)
    review_path = output_dir / "library.review.json"
    review: dict[str, Any] = {"schema_version": 1, "import_schema_version": IMPORT_SCHEMA_VERSION,
                            "provider": provider_identity(config), "mode": mode, "human_reviewed": False, "jobs": [], "errors": [],
                            "statistics": {"selected": 0, "success": 0, "resumed": 0, "preserved_reviewed": 0, "failed": 0}}
    prepared = []
    # Validate all requested sources before spending API calls. Paths only come from DB.
    with session_factory() as db:
        for spec in specs:
            document = db.get(SourceDocument, spec["document_id"])
            if document is None:
                raise ValueError(f"教材不存在：{spec['document_id']}")
            path = Path(document.path).resolve()
            if path.suffix.lower() != ".pdf" or not path.is_file() or sha256_path(path) != document.file_hash:
                raise ValueError(f"教材文件不存在、非 PDF 或哈希变化：doc={document.id}")
            for field in ("file_hash", "page_count", "course_id"):
                if field in spec and spec[field] != getattr(document, field):
                    raise ValueError(f"书清单身份不匹配：doc={document.id}, field={field}")
            if document.course_id is None:
                raise ValueError(f"教材没有课程：doc={document.id}")
            import fitz
            with fitz.open(path) as pdf:
                if len(pdf) != document.page_count:
                    raise ValueError(f"教材页数不匹配：doc={document.id}")
            selected = sorted(set(pages)) if pages is not None else list(range(1, document.page_count + 1))
            if any(page > document.page_count for page in selected):
                raise ValueError(f"所选页超过教材页数：doc={document.id}")
            job = db.scalar(select(IngestionJob).where(IngestionJob.source_document_id == document.id).order_by(IngestionJob.id.desc()))
            if job is None:
                raise ValueError(f"教材缺少原始导入任务：doc={document.id}")
            items = list(db.scalars(select(ExtractedItem).where(ExtractedItem.ingestion_job_id == job.id).order_by(ExtractedItem.id)))
            by_page: dict[int, ExtractedItem] = {}
            for item in items:
                if item.page_number in by_page:
                    raise ValueError(f"同页存在多个解析项，需先人工解决：doc={document.id}, page={item.page_number}")
                by_page[item.page_number] = item
            if any(page not in by_page for page in selected):
                raise ValueError(f"教材缺少原始页解析项：doc={document.id}")
            job_spec = {"job_id": job.id, "course_id": document.course_id, "source_document_id": document.id,
                        "path": str(path), "file_hash": document.file_hash, "review_items": [_review_snapshot(by_page[p]) for p in selected],
                        "auto_approved": 0, "errors": [], "selected_pages": selected}
            review["jobs"].append(job_spec)
            review["statistics"]["selected"] += len(selected)
            document_data = {"id": document.id, "course_id": document.course_id, "filename": document.filename,
                             "path": str(path), "file_hash": document.file_hash, "page_count": document.page_count, "parser": document.parser}
            prepared.append((document_data, [{"id": by_page[p].id, "page_number": p, "raw_text": by_page[p].raw_text or ""} for p in selected], job_spec))
    _atomic_json(review_path, review)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="library-transcription") as pool:
        for document_data, items, job_spec in prepared:
            pending = {}
            iterator = iter(items)
            snapshots = {item["page_number"]: item for item in job_spec["review_items"]}
            finished = 0
            def submit_next() -> bool:
                item = next(iterator, None)
                if item is None:
                    return False
                future = pool.submit(_prepare_page, document_data, item, output_dir=output_dir, config=config,
                                     mode=mode, force=force, retries=retries, max_output_tokens=max_output_tokens)
                pending[future] = item
                return True
            for _ in range(workers):
                submit_next()
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    item = pending.pop(future)
                    try:
                        record = future.result()
                        snapshot = _apply_page(document_data["id"], item["id"], record, session_factory)
                        snapshots[item["page_number"]] = snapshot
                        status = snapshot["import_status"]
                        review["statistics"][status] += 1
                    except Exception as error:
                        status = "failed"
                        diagnostic = {"source_document_id": document_data["id"], "page_number": item["page_number"],
                                      "error_type": error.__class__.__name__}
                        if isinstance(error, (AssistUnavailable, LocalOCRFailure)):
                            diagnostic["reason"] = str(error)
                            diagnostic["upstream"] = getattr(error, "upstream_diagnostics", {})
                        if isinstance(error, LocalOCRFailure):
                            diagnostic["blank_evidence"] = error.blank_evidence
                        retryable, delay = _retry_details(error)
                        diagnostic.update({"retryable": retryable, "retry_after": delay})
                        job_spec["errors"].append(diagnostic)
                        review["errors"].append(diagnostic)
                        snapshots[item["page_number"]]["import_status"] = "failed"
                        review["statistics"]["failed"] += 1
                    finished += 1
                    job_spec["review_items"] = [snapshots[p] for p in job_spec["selected_pages"]]
                    _atomic_json(review_path, review)
                    progress(f"doc={document_data['id']} page={item['page_number']}/{document_data['page_count']} {status} ({finished}/{len(items)})")
                    submit_next()
    review["completed"] = review["statistics"]["failed"] == 0
    review["review_manifest"] = str(review_path)
    _atomic_json(review_path, review)
    return review
