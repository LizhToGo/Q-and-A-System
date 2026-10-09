from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from backend.app.db import SessionLocal, create_tables
from backend.app.services.ingestion.assist import AssistUnavailable, assist_extracted_item
from backend.app.services.ingestion.service import create_ingestion_job, list_job_items, ocr_item, publish_job


SCHEMA_VERSION = 1
_NUMERIC_HINT = re.compile(r"\d|[０-９零〇一二三四五六七八九十百千万亿两半]|百分之|倍|[%％]|(?:kHz|MHz|GHz|Hz|V|A|Ω|W|dB|ms|μs|s)\b", re.I)
_FORMULA_HINT = re.compile(r"[A-Za-z][A-Za-z0-9_{}]*\s*[=<>≥≤]")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"清单必须是 JSON 对象：{path}")
    return value


def _resolve_path(raw_path: str, anchor: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    candidates = [anchor / path, Path.cwd() / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def _pages(value: Any, *, field: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or any(isinstance(page, bool) or not isinstance(page, int) or page < 1 for page in value):
        raise ValueError(f"{field} 必须是正整数数组")
    return sorted(set(value))


def _is_low_risk_text(item: Any) -> bool:
    payload = item.payload if isinstance(item.payload, dict) else {}
    raw_text = (item.raw_text or "").strip()
    normalized_text = (item.normalized_text or "").strip()
    try:
        confidence = float(item.confidence or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return bool(
        item.item_type == "text"
        and len(raw_text) >= 80
        and normalized_text == raw_text
        and not _NUMERIC_HINT.search(raw_text)
        and not _FORMULA_HINT.search(raw_text)
        and not payload.get("needs_ocr")
        and not payload.get("ocr_engine")
        and not payload.get("llm_provider")
        and not payload.get("formulas")
        and not payload.get("images")
        and confidence >= 0.7
    )


def _item_snapshot(item: Any, approved: bool = False, approval_method: str | None = None) -> dict[str, Any]:
    approved = approved or item.review_status == "approved"
    return {
        "item_id": item.id,
        "page_number": item.page_number,
        "source_anchor": item.source_anchor,
        "item_type": item.item_type,
        "raw_text": item.raw_text or "",
        "normalized_text": item.normalized_text or "",
        "source_text_sha256": _sha256_text(item.raw_text or ""),
        "payload": item.payload or {},
        "confidence": item.confidence,
        "review_status": item.review_status,
        "approved": approved,
        "approval_method": approval_method or ("existing" if approved else "pending"),
    }


def build_review_manifest(manifest_path: Path, db: Any, *, strict_assist: bool = False) -> dict[str, Any]:
    source_manifest_bytes = manifest_path.read_bytes()
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValueError(f"不支持的清单版本：{manifest.get('schema_version')}")
    jobs_spec = manifest.get("jobs")
    if not isinstance(jobs_spec, list) or not jobs_spec:
        raise ValueError("清单必须包含非空 jobs 数组")

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_manifest_sha256": _sha256_bytes(source_manifest_bytes),
        "jobs": [],
    }
    for spec in jobs_spec:
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str) or not isinstance(spec.get("course_id"), int):
            raise ValueError("每个 job 必须包含 path 和整数 course_id")
        path = _resolve_path(spec["path"], manifest_path.parent)
        course_id = spec["course_id"]
        job = create_ingestion_job(db, path, course_id)
        items_by_page = {item.page_number: item for item in list_job_items(db, job.id)}
        ocr_pages = _pages(spec.get("ocr_pages"), field="ocr_pages")
        assist_pages = _pages(spec.get("assist_pages"), field="assist_pages")
        review_pages = _pages(spec.get("review_pages", spec.get("pages")), field="review_pages")
        errors: list[dict[str, Any]] = []

        for page in ocr_pages:
            item = items_by_page.get(page)
            if item is None:
                raise ValueError(f"任务 {job.id} 不存在第 {page} 页")
            if item.raw_text and not (item.payload or {}).get("needs_ocr") and not spec.get("force_ocr", False):
                continue
            try:
                ocr_item(db, item.id)
            except Exception as exc:
                errors.append({"page_number": page, "stage": "ocr", "error": exc.__class__.__name__})
                if strict_assist:
                    raise

        for page in assist_pages:
            item = items_by_page.get(page)
            if item is None:
                raise ValueError(f"任务 {job.id} 不存在第 {page} 页")
            try:
                assist_extracted_item(db, item.id, mode=str(spec.get("assist_mode", "auto")))
            except AssistUnavailable as exc:
                errors.append({"page_number": page, "stage": "assist", "error": str(exc)})
                if strict_assist:
                    raise

        db.refresh(job)
        items_by_page = {item.page_number: item for item in list_job_items(db, job.id)}
        if not review_pages:
            review_pages = sorted(items_by_page)
        selected = []
        auto_approved = 0
        auto_approve_low_risk = bool(spec.get("auto_approve_low_risk", False))
        for page in review_pages:
            item = items_by_page.get(page)
            if item is None:
                raise ValueError(f"任务 {job.id} 不存在第 {page} 页")
            if item.review_status == "approved":
                selected.append(_item_snapshot(item, approval_method="existing"))
            elif auto_approve_low_risk and _is_low_risk_text(item):
                selected.append(_item_snapshot(item, approved=True, approval_method="auto_low_risk_text"))
                auto_approved += 1
            else:
                selected.append(_item_snapshot(item))
        result["jobs"].append({
            "job_id": job.id,
            "course_id": course_id,
            "source_document_id": job.source_document_id,
            "path": spec["path"],
            "file_hash": _sha256_file(path),
            "review_items": selected,
            "auto_approved": auto_approved,
            "errors": errors,
        })
    return result


def apply_review_manifest(review_path: Path, db: Any, *, publish: bool) -> dict[str, Any]:
    review = _load_json(review_path)
    if review.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"不支持的审核清单版本：{review.get('schema_version')}")
    summaries = []
    for job_spec in review.get("jobs", []):
        path = _resolve_path(str(job_spec["path"]), review_path.parent)
        if _sha256_file(path) != job_spec.get("file_hash"):
            raise ValueError(f"教材文件哈希已变化：{path}")
        job = create_ingestion_job(db, path, int(job_spec["course_id"]))
        items_by_page = {item.page_number: item for item in list_job_items(db, job.id)}
        approved = 0
        for snapshot in job_spec.get("review_items", []):
            if not snapshot.get("approved", False):
                continue
            page = int(snapshot["page_number"])
            item = items_by_page.get(page)
            if item is None or item.source_anchor != f"doc:{job.source_document_id}:p{page}":
                raise ValueError(f"审核项来源不匹配：job={job.id}, page={page}")
            if _sha256_text(item.raw_text or "") != snapshot.get("source_text_sha256"):
                raise ValueError(f"原始文本已变化：job={job.id}, page={page}")
            normalized_text = str(snapshot.get("normalized_text") or "").strip()
            if not normalized_text:
                raise ValueError(f"审核项内容为空：job={job.id}, page={page}")
            item.normalized_text = normalized_text
            if isinstance(snapshot.get("payload"), dict):
                item.payload = snapshot["payload"]
            item.review_status = "approved"
            item.needs_review = False
            approved += 1
        db.commit()
        published = publish_job(db, job.id, int(job_spec["course_id"])) if publish else {"published": 0, "skipped": 0}
        summaries.append({"job_id": job.id, "approved": approved, **published})
    return {"jobs": summaries, "published": publish}


def main() -> None:
    parser = argparse.ArgumentParser(description="构建可重复、可审核、可发布的教材知识库")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest", type=Path, help="输入教材构建清单，生成待审核 review manifest")
    group.add_argument("--approve-manifest", type=Path, help="人工审核后的 review manifest")
    parser.add_argument("--output", type=Path, help="构建阶段输出的审核清单路径")
    parser.add_argument("--publish", action="store_true", help="应用审核清单后发布 approved 项")
    parser.add_argument("--strict-assist", action="store_true", help="模型辅助失败时终止构建；默认保留 pending 项并继续")
    args = parser.parse_args()

    create_tables()
    db = SessionLocal()
    try:
        if args.manifest:
            output = args.output or args.manifest.with_suffix(".review.json")
            result = build_review_manifest(args.manifest.resolve(), db, strict_assist=args.strict_assist)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            print(json.dumps({"mode": "build", "output": str(output), "jobs": result["jobs"]}, ensure_ascii=False))
        else:
            result = apply_review_manifest(args.approve_manifest.resolve(), db, publish=args.publish)
            print(json.dumps({"mode": "approve", **result}, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
