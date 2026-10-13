from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.db import SessionLocal
from backend.app.models import Material
from backend.build_knowledge import _is_low_risk_text, apply_review_manifest, build_review_manifest



def test_review_manifest_requires_explicit_approval_and_is_idempotent(client, tmp_path):
    source = tmp_path / "chapter.md"
    source.write_text("1.1 采样定理\n\n抽样频率必须大于信号最高频率的两倍。", encoding="utf-8")
    manifest_path = tmp_path / "build.json"
    manifest_path.write_text(json.dumps({"schema_version": 1, "jobs": [{"path": "chapter.md", "course_id": 1, "review_pages": [1], "ocr_pages": [], "assist_pages": []}]}), encoding="utf-8")

    db = SessionLocal()
    try:
        review = build_review_manifest(manifest_path, db)
        assert review["jobs"][0]["review_items"][0]["approved"] is False
        review_path = tmp_path / "review.json"
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        first = apply_review_manifest(review_path, db, publish=True)
        assert first["jobs"][0]["published"] == 0

        review["jobs"][0]["review_items"][0]["approved"] = True
        review["jobs"][0]["review_items"][0]["payload"]["section"] = "1.1 采样定理"
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        published = apply_review_manifest(review_path, db, publish=True)
        assert published["jobs"][0]["published"] == 1
        repeated = apply_review_manifest(review_path, db, publish=True)
        assert repeated["jobs"][0]["published"] == 0
        source_document_id = review["jobs"][0]["source_document_id"]
        assert len(list(db.scalars(select(Material).where(Material.source_document_id == source_document_id)))) == 1
        from backend.app.services.ingestion.service import build_rag_index
        from backend.app.services.retrieval import search_evidence
        material = db.scalar(select(Material).where(Material.source_document_id == source_document_id))
        published_topic = material.topic_id
        assert published_topic is not None
        build_rag_index(db, review["jobs"][0]["job_id"])
        matches = search_evidence(db, "采样定理", course_id=1, topic_id=published_topic, source_document_id=source_document_id)
        assert [match.material_id for match in matches] == [material.id]

        source.write_text("changed source", encoding="utf-8")
        with pytest.raises(ValueError, match="哈希已变化"):
            apply_review_manifest(review_path, db, publish=True)
    finally:
        db.close()


def test_low_risk_native_text_can_be_auto_approved(client, tmp_path):
    source = tmp_path / "clean.md"
    source.write_text(
        "采样定理\n\n"
        "本节介绍基础课程概念，说明教材正文、章节标题和普通说明文字可以保持原文不变，"
        "并且不依赖特殊符号或外部处理即可进入低风险自动审核路径，审核后仍保留原始来源锚点。",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "build.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jobs": [
                    {
                        "path": "clean.md",
                        "course_id": 1,
                        "review_pages": [1],
                        "auto_approve_low_risk": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    db = SessionLocal()
    try:
        review = build_review_manifest(manifest_path, db)
        item = review["jobs"][0]["review_items"][0]
        assert item["approved"] is True
        assert item["approval_method"] == "auto_low_risk_text"
        assert review["jobs"][0]["auto_approved"] == 1
        review_path = tmp_path / "review.json"
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        published = apply_review_manifest(review_path, db, publish=True)
        assert published["jobs"][0]["published"] == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    "item",
    [
        SimpleNamespace(
            item_type="formula",
            raw_text="普通教材说明文字足够长但类型是公式。",
            normalized_text="普通教材说明文字足够长但类型是公式。",
            payload={},
            confidence=0.95,
        ),
        SimpleNamespace(
            item_type="text",
            raw_text="采样频率 fs=8000 Hz，包含数字和公式。",
            normalized_text="采样频率 fs=8000 Hz，包含数字和公式。",
            payload={},
            confidence=0.95,
        ),
        SimpleNamespace(
            item_type="text",
            raw_text="这是一段足够长的 OCR 文本，不能因为模型置信度较高就自动发布。",
            normalized_text="这是一段足够长的 OCR 文本，不能因为模型置信度较高就自动发布。",
            payload={"needs_ocr": True},
            confidence=0.95,
        ),
    ],
)
def test_low_risk_gate_rejects_risky_items(item):
    assert _is_low_risk_text(item) is False


def test_low_risk_gate_rejects_chinese_quantity_words():
    text = "该信号幅度提高两倍后仍需检查单位和边界，不能仅按普通正文自动发布。"
    item = SimpleNamespace(
        item_type="text",
        raw_text=text,
        normalized_text=text,
        payload={},
        confidence=0.95,
    )

    assert _is_low_risk_text(item) is False


def test_low_risk_gate_allows_ordinary_chinese_words():
    text = "一般来说，一个基础概念的普通说明文字可以保持原文不变并进入自动审核路径，且没有数字或单位。"
    text += "这类内容只需要保留来源锚点和原始文本哈希，不包含计算、测量或图形解释。"
    item = SimpleNamespace(
        item_type="text",
        raw_text=text,
        normalized_text=text,
        payload={},
        confidence=0.95,
    )

    assert _is_low_risk_text(item) is True


def test_blank_approval_rechecks_original_and_never_creates_search_text(client, tmp_path):
    import fitz
    from backend.app.models import ExtractedItem, SourceDocument
    from backend.app.services.ingestion.library_import import _blank_evidence

    source = tmp_path / "blank.pdf"
    with fitz.open() as pdf:
        pdf.new_page()
        pdf.new_page().insert_text((30, 30), "Not a blank page")
        pdf.save(source)
    manifest = tmp_path / "build.json"
    manifest.write_text(json.dumps({"schema_version": 1, "jobs": [{"path": str(source), "course_id": 1}]}))
    with SessionLocal() as db:
        review = build_review_manifest(manifest, db)
        job = review["jobs"][0]
        document = db.get(SourceDocument, job["source_document_id"])
        first, second = job["review_items"]
        first.update(approved=True, item_type="blank", normalized_text="", quality_review_cleared=True)
        first["payload"]["blank_evidence"] = _blank_evidence(document, 1)
        path = tmp_path / "review.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        result = apply_review_manifest(path, db, publish=True)
        assert result["jobs"][0]["approved_blank"] == 1
        item = db.scalar(select(ExtractedItem).where(ExtractedItem.source_anchor == f"doc:{document.id}:p1"))
        assert item.review_status == "approved" and not item.needs_review
        assert item.normalized_text == ""
        assert db.scalar(select(Material).where(Material.source_document_id == document.id)) is None

        second.update(approved=True, item_type="blank", normalized_text="", quality_review_cleared=True)
        second["payload"]["blank_evidence"] = first["payload"]["blank_evidence"]
        path.write_text(json.dumps(review), encoding="utf-8")
        with pytest.raises(ValueError, match="空白页与原页不符"):
            apply_review_manifest(path, db, publish=True)


def test_visually_reviewed_scanned_blank_requires_matching_original_raster(client, tmp_path):
    import fitz
    from backend.app.models import SourceDocument
    from backend.app.services.ingestion.library_import import _blank_evidence
    from backend.app.services.ingestion.service import build_rag_index, get_rag_index_status

    source = tmp_path / "scanned-blank.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.draw_rect(fitz.Rect(0, 0, 10, page.rect.height), color=None, fill=(0.8, 0.8, 0.8))
        pdf.save(source)
    manifest = tmp_path / "build.json"
    manifest.write_text(json.dumps({"schema_version": 1, "jobs": [{"path": str(source), "course_id": 1}]}))
    with SessionLocal() as db:
        review = build_review_manifest(manifest, db)
        job = review["jobs"][0]
        document = db.get(SourceDocument, job["source_document_id"])
        snapshot = job["review_items"][0]
        evidence = _blank_evidence(document, 1)
        assert not evidence["is_blank"]
        snapshot.update(approved=True, item_type="blank", normalized_text="", quality_review_cleared=True)
        snapshot["payload"].update({
            "blank_evidence": evidence,
            "empty_page_review": {
                "method": "visual_original_pdf", "kind": "blank", "visual_reviewed": True,
                "notes": "原页只有扫描灰边，没有可读正文。", "source_raster_sha256": "wrong",
            },
        })
        path = tmp_path / "review.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        with pytest.raises(ValueError, match="空白页与原页不符"):
            apply_review_manifest(path, db, publish=True)
        snapshot["payload"]["empty_page_review"]["source_raster_sha256"] = evidence["raster_sha256"]
        path.write_text(json.dumps(review), encoding="utf-8")
        assert apply_review_manifest(path, db, publish=True)["jobs"][0]["approved_blank"] == 1
        assert build_rag_index(db, job["job_id"])["status"] == "published"
        status = get_rag_index_status(db, job["job_id"])
        assert status["reviewed_blank_pages"] == 1 and status["review_coverage"] == 1
        assert status["indexed_pages"] == 0 and status["chunk_count"] == 0
