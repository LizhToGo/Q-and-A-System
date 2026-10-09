from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from backend.app.db import SessionLocal
from backend.app.models import Material
from backend.build_knowledge import apply_review_manifest, build_review_manifest


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
