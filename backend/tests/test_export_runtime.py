from __future__ import annotations

import json
import os
import sqlite3
from io import BytesIO
from types import SimpleNamespace

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import export_runtime as exporter
from backend.app.db import Base
from backend.app.models import (
    AnswerRecord, Conversation, ConversationTurn, Course, EvidenceLink, ExtractedItem,
    Formula, IngestionJob, Material, MaterialChunk, SourceDocument, ValidationResultRecord,
)
from backend.app.services.ingestion.service import sha256_path


def _rows(database):
    with sqlite3.connect(database) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() for table in tables}


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "source"
    data = root / "data"
    data.mkdir(parents=True)
    database = data / "knowledge.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    Base.metadata.create_all(engine)
    books = []
    with Session(engine) as db:
        db.add(Course(id=1, name="教材课程"))
        for document_id in range(1, 5):
            book = root / f"原教材 {document_id}.pdf"
            with fitz.open() as pdf:
                page = pdf.new_page(width=200, height=200)
                page.insert_text((20, 40), f"Original textbook {document_id}")
                page.draw_rect(fitz.Rect(20, 80, 160, 160), color=(1, 0, 0), fill=(1, 0, 0))
                pdf.save(book)
            books.append(book)
            db.add(SourceDocument(id=document_id, course_id=1, filename=book.name, path=str(book),
                                  file_hash=sha256_path(book), page_count=1, parser="pymupdf", status="completed"))
        db.add(IngestionJob(id=1, source_document_id=1, status="completed", total_pages=1, processed_pages=1))
        db.add(ExtractedItem(id=1, ingestion_job_id=1, page_number=1, item_type="page",
                            raw_text="原始不可变文本", normalized_text="人工审核内容", payload={"human_note": "保留"},
                            review_status="approved", needs_review=False, source_anchor="doc:1:p1"))
        db.add(Material(id=1, course_id=1, source_document_id=1, material_type="text", title="审核页",
                        content="人工确认内容与公式 x=1", page_number=1, source_anchor="doc:1:p1", verified=True,
                        metadata_json={"review_manifest_file": "data/reviews/page.review.json",
                                       "publication_status": "published", "review_status": "approved",
                                       "figures": [{"label": "图1", "bbox": [0.1, 0.4, 0.8, 0.8],
                                                    "review_status": "approved", "page_number": 1}]}))
        db.add(MaterialChunk(id=1, material_id=1, chunk_index=0, text="检索正文 x=1"))
        db.add(Formula(id=1, material_id=1, raw_formula="x=1", canonical_expression="x=1", variables_json={"x": 1}))
        db.add(AnswerRecord(id=1, question="已有问题", answer="已有回答", course_id=1,
                            result_json={"artifacts": [{"url": "/api/tool-artifacts/" + "a" * 32}]}))
        db.add(Conversation(id=1, title="已有对话", title_source="user", course_id=1, source_document_id=1))
        db.add(ConversationTurn(id=1, conversation_id=1, answer_id=1))
        db.add(ValidationResultRecord(id=1, answer_id=1, category="textbook", status="supported", message="已审核"))
        db.add(EvidenceLink(id=1, answer_id=1, material_id=1, chunk_id=1))
        db.commit()
    engine.dispose()
    review = data / "reviews" / "page.review.json"
    review.parent.mkdir()
    review.write_text(json.dumps({"approved": True, "human_note": "保留审核结果"}, ensure_ascii=False), encoding="utf-8")
    artifacts = data / "tool_artifacts"
    artifacts.mkdir()
    Image.new("RGB", (12, 8), "red").save(artifacts / ("a" * 32 + ".png"))
    env = root / ".env"
    env.write_text("LLM_API_KEY=test-only-environment-secret\nLLM_CONFIG_SOURCE=saved\n", encoding="utf-8")
    config = data / "provider-config.json"
    config.write_text(json.dumps({"providers": [{"id": "saved", "enabled": True, "provider": "openai",
                                               "base_url": "https://example.invalid/v1/responses", "model": "saved-model",
                                               "api_key": "test-only-provider-secret"}],
                                  "default_provider_id": "saved", "default_provider_explicit": True}), encoding="utf-8")
    token = data / "admin-token"
    token.write_text("test-only-admin-secret", encoding="utf-8")
    live = sqlite3.connect(database)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("INSERT INTO answer_records SELECT 2, 'committed live question', answer, course_id, topic_id, "
                 "result_json, created_at FROM answer_records WHERE id=1")
    live.commit()
    state = SimpleNamespace(root=root, database=database, books=books, review=review, artifacts=artifacts,
                            config=config, env=env, token=token, live=live, output=tmp_path / "runtime")
    yield state
    live.close()


def test_consistent_live_snapshot_preserves_all_data_and_portable_originals(runtime):
    source_rows = _rows(runtime.database)
    book_hashes = [sha256_path(book) for book in runtime.books]
    runtime.live.execute("INSERT INTO answer_records SELECT 3, 'uncommitted question', answer, course_id, topic_id, "
                         "result_json, created_at FROM answer_records WHERE id=1")
    try:
        manifest = exporter.export_runtime(runtime.output, database=runtime.database, root=runtime.root)
    finally:
        runtime.live.rollback()
    assert manifest["completed"] is True
    assert manifest["table_counts"]["source_documents"] == 4
    assert manifest["table_counts"]["answer_records"] == 2
    copied_rows = _rows(runtime.output / "data" / "knowledge.db")
    for table, rows in source_rows.items():
        if table != "source_documents":
            assert copied_rows[table] == rows
    for original, copied in zip(source_rows["source_documents"], copied_rows["source_documents"]):
        # id, course_id, filename, hash remain immutable; only path is migrated.
        assert copied[:4] == original[:4]
        assert copied[5:] == original[5:]
        assert copied[4].startswith("/app/data/uploads/doc-")
        relative = copied[4].removeprefix("/app/")
        assert relative.isascii()
        book = runtime.output / relative
        assert sha256_path(book) == original[3]
        with fitz.open(book) as pdf:
            assert f"Original textbook {original[0]}" in pdf[0].get_text()
            assert pdf[0].get_pixmap().width == 200
    assert _rows(runtime.database) == source_rows
    assert [sha256_path(book) for book in runtime.books] == book_hashes
    saved_manifest = json.loads((runtime.output / "runtime-manifest.json").read_text(encoding="utf-8"))
    assert saved_manifest == manifest
    assert (runtime.output / "data/reviews/page.review.json").read_bytes() == runtime.review.read_bytes()
    for record in manifest["files"]:
        exported = runtime.output / record["path"]
        assert record["sha256"] == sha256_path(exported)
        assert record["size_bytes"] == exported.stat().st_size


def test_original_page_review_crop_and_historical_tool_png_remain_consumable(runtime):
    from backend.app import source_media, tool_artifacts

    exporter.export_runtime(runtime.output, database=runtime.database, root=runtime.root)
    engine = create_engine(f"sqlite:///{(runtime.output / 'data/knowledge.db').as_posix()}")
    try:
        with Session(engine) as db:
            document = db.get(SourceDocument, 1)
            db.get(Material, 1)
            # Map the container's bind-mount path into the test host without
            # committing any change to the exported database.
            document.path = str(runtime.output / document.path.removeprefix("/app/"))
            with db.no_autoflush:
                page = source_media.get_page_image(1, 1, db)
                crop = source_media.get_material_media(1, "figures", 0, db)
            with Image.open(BytesIO(page.body)) as original, Image.open(BytesIO(crop.body)) as cropped:
                assert cropped.width < original.width and cropped.height < original.height
                assert cropped.getpixel((cropped.width // 2, cropped.height // 2))[:3] == (255, 0, 0)
    finally:
        engine.dispose()
    app = FastAPI()
    app.include_router(tool_artifacts.router)
    app.dependency_overrides[tool_artifacts.get_artifact_directory] = lambda: runtime.output / "data/tool_artifacts"
    with TestClient(app) as client:
        response = client.get("/api/tool-artifacts/" + "a" * 32)
        assert response.status_code == 200
        with Image.open(BytesIO(response.content)) as image:
            assert image.size == (12, 8)


def test_configuration_is_opt_in_private_and_still_usable(runtime, monkeypatch):
    from backend.app import provider_config

    config_hashes = [sha256_path(path) for path in (runtime.env, runtime.config, runtime.token)]
    default_manifest = exporter.export_runtime(runtime.output, database=runtime.database, root=runtime.root)
    for entry in default_manifest["configuration"]:
        assert entry["present"] and not entry["included"]
        assert not (runtime.output / entry["path"]).exists()
    private_output = runtime.output.with_name("private-runtime")
    manifest = exporter.export_runtime(private_output, database=runtime.database, root=runtime.root, include_config=True)
    for entry in manifest["configuration"]:
        assert entry["included"]
        assert entry["sha256"] == sha256_path(private_output / entry["path"])
    monkeypatch.setattr(provider_config, "PROVIDER_CONFIG_PATH", private_output / "data/provider-config.json")
    monkeypatch.setattr(provider_config, "ADMIN_TOKEN_PATH", private_output / "data/admin-token")
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    config = provider_config.get_provider_config(SimpleNamespace(llm_config_source="saved", llm_provider="none"))
    assert config.model == "saved-model" and config.api_key == "test-only-provider-secret"
    assert provider_config.get_admin_token() == "test-only-admin-secret"
    for manifest_path in (runtime.output / "runtime-manifest.json", private_output / "runtime-manifest.json"):
        assert "test-only-" not in manifest_path.read_text(encoding="utf-8")
    assert [sha256_path(path) for path in (runtime.env, runtime.config, runtime.token)] == config_hashes
    if os.name != "nt":
        assert private_output.stat().st_mode & 0o777 == 0o700
        for entry in manifest["configuration"]:
            assert (private_output / entry["path"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure,code", [("missing", "missing_book"), ("corrupt", "book_hash_mismatch")])
def test_missing_or_corrupted_original_never_leaves_completed_package(runtime, failure, code):
    source_rows = _rows(runtime.database)
    if failure == "missing":
        runtime.books[-1].unlink()
    else:
        runtime.books[-1].write_bytes(b"changed original textbook")
    with pytest.raises(exporter.RuntimeExportError) as caught:
        exporter.export_runtime(runtime.output, database=runtime.database, root=runtime.root, include_config=True)
    assert caught.value.code == code
    assert "4" in str(caught.value)
    assert not runtime.output.exists()
    assert _rows(runtime.database) == source_rows
    assert runtime.config.exists() and runtime.env.exists() and runtime.token.exists()


def test_existing_output_is_never_overwritten_or_cleaned(runtime):
    runtime.output.mkdir()
    sentinel = runtime.output / "runtime-manifest.json"
    sentinel.write_bytes(b"existing user deployment")
    with pytest.raises(exporter.RuntimeExportError) as caught:
        exporter.export_runtime(runtime.output, database=runtime.database, root=runtime.root)
    assert caught.value.code == "output_exists"
    assert sentinel.read_bytes() == b"existing user deployment"
    assert list(runtime.output.iterdir()) == [sentinel]


def test_referenced_private_configuration_cannot_bypass_opt_in(runtime):
    runtime.live.execute("UPDATE materials SET metadata_json=? WHERE id=1",
                         (json.dumps({"review_manifest_file": "data/provider-config.json"}),))
    runtime.live.commit()
    with pytest.raises(exporter.RuntimeExportError) as caught:
        exporter.export_runtime(runtime.output, database=runtime.database, root=runtime.root)
    assert caught.value.code == "unsafe_review_path"
    assert not runtime.output.exists()


def test_cli_returns_consumer_visible_completion_and_safe_failure(runtime, monkeypatch, capsys):
    monkeypatch.setattr(exporter, "ROOT_DIR", runtime.root)
    assert exporter.main(["--output", str(runtime.output), "--database", str(runtime.database), "--include-config"]) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["completed"] and success["documents"] == 4 and success["config_included"]
    runtime.books[-1].write_bytes(b"invalid textbook")
    failed_output = runtime.output.with_name("failed-runtime")
    assert exporter.main(["--output", str(failed_output), "--database", str(runtime.database)]) == 2
    text = capsys.readouterr().out
    failure = json.loads(text)
    assert not failure["completed"] and failure["error_code"] == "book_hash_mismatch"
    assert "test-only-" not in text and str(runtime.root) not in text
    assert not failed_output.exists()
