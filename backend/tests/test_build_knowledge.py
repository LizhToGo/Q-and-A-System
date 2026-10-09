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
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        published = apply_review_manifest(review_path, db, publish=True)
        assert published["jobs"][0]["published"] == 1
        repeated = apply_review_manifest(review_path, db, publish=True)
        assert repeated["jobs"][0]["published"] == 0
        source_document_id = review["jobs"][0]["source_document_id"]
        assert len(list(db.scalars(select(Material).where(Material.source_document_id == source_document_id)))) == 1

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
