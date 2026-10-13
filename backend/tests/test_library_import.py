from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import Course, ExtractedItem, IngestionJob, Material, SourceDocument
from backend.app.provider_config import ProviderConfig
from backend.app.services.ingestion import library_import as importer
from backend.app.services.ingestion import page_transcription
from backend.app.services.ingestion.service import sha256_path
from backend.build_knowledge import apply_review_manifest
from backend.import_library import main


@pytest.fixture
def library(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    pdf_path = tmp_path / "actual.pdf"
    with fitz.open() as pdf:
        for page in range(2):
            pdf.new_page().insert_text((50, 50), f"Page {page + 1}: original native textbook content " * 4)
        pdf.save(pdf_path)
    with factory() as db:
        db.add(Course(id=2, name="library"))
        db.add(SourceDocument(id=2, course_id=2, filename=pdf_path.name, path=str(pdf_path),
                              file_hash=sha256_path(pdf_path), page_count=2, parser="pymupdf"))
        db.add(IngestionJob(id=2, source_document_id=2, total_pages=2, processed_pages=2))
        for page in (1, 2):
            db.add(ExtractedItem(id=page, ingestion_job_id=2, page_number=page, item_type="page",
                                raw_text=f"immutable original {page}", normalized_text=f"immutable original {page}",
                                payload={"needs_ocr": True}, confidence=0.1, review_status="pending",
                                needs_review=True, source_anchor=f"doc:2:p{page}"))
        db.commit()
    config = ProviderConfig(id="front", config_source="saved", enabled=True, provider="openai",
                            base_url="https://example.invalid/v1", model="current-front-model")
    monkeypatch.setattr(importer, "get_provider_config", lambda **kwargs: config)
    monkeypatch.setattr(importer, "_pdf_quality", lambda page, native: {"quality_flags": ["ocr_required"]})
    ocr_calls = []
    model_calls = []
    def ocr(document, page):
        ocr_calls.append(page)
        return {"raw_text": f"actual OCR {page}", "blocks": [{"text": f"actual OCR {page}", "bbox": [[0, 0], [10, 0], [10, 10], [0, 10]]}], "confidence": 0.8}
    def transcribe(document, page, **kwargs):
        model_calls.append((page, kwargs))
        return {"source_page": page, "normalized_text": f"Complete page {page}\n" + "full content " * 1000,
                "item_type": "text", "payload": {"section": "Section", "content_format": "markdown",
                    "source_anchor": "forged", "source_bounds": [0, 0, 999, 999], "file_hash": "forged",
                    "figures": [{"label": "Fig", "page_number": 999, "source_anchor": "forged", "bbox": [-1, 0, 2, 1]}],
                    "formulas": [r"\(x=1\)"], "reviewed_formulas": [], "uncertainties": []},
                "confidence": 0.99, "warnings": [], "diagnostics": {"mode": "text"}}
    monkeypatch.setattr(importer, "ocr_page", ocr)
    monkeypatch.setattr(page_transcription, "transcribe_page", transcribe)
    state = SimpleNamespace(factory=factory, output=tmp_path / "output", config=config, ocr_calls=ocr_calls,
                            model_calls=model_calls, transcribe=transcribe, pdf_path=pdf_path)
    yield state
    engine.dispose()


def run(library, **kwargs):
    return importer.import_library([{"document_id": 2}], output_dir=library.output,
                                   session_factory=library.factory, progress=lambda line: None, mode=kwargs.pop("mode", "text"), **kwargs)


def test_full_text_pending_provenance_and_resume(library):
    first = run(library)
    assert first["completed"] and first["statistics"]["success"] == 2
    with library.factory() as db:
        item = db.get(ExtractedItem, 1)
        assert item.raw_text == "immutable original 1"
        assert len(item.normalized_text) > 12000
        assert item.review_status == "pending" and item.needs_review
        assert item.payload["source_anchor"] == "doc:2:p1"
        assert item.payload["file_hash"] == sha256_path(library.pdf_path)
        assert item.payload["source_bounds"] != [0, 0, 999, 999]
        assert item.payload["ocr_text"] == "actual OCR 1" and item.payload["ocr_boxes"]
        figure = item.payload["figures"][0]
        assert figure["page_number"] == 1 and "source_anchor" not in figure and "bbox" not in figure
        material = db.scalar(select(Material).where(Material.page_number == 1))
        assert material.content == item.normalized_text and not material.verified
        assert material.metadata_json["publication_status"] == "indexed_unverified"
        material_id = material.id
    second = run(library)
    assert second["statistics"]["resumed"] == 2
    assert len(library.model_calls) == len(library.ocr_calls) == 2
    with library.factory() as db:
        assert db.scalar(select(Material).where(Material.page_number == 1)).id == material_id
    assert all(not item["approved"] for item in second["jobs"][0]["review_items"])


def test_preserves_manual_pending_and_approved_even_with_force(library):
    run(library)
    with library.factory() as db:
        item = db.get(ExtractedItem, 1)
        item.normalized_text = "human edited pending page"
        item.payload = {**item.payload, "human_note": "keep"}
        approved = db.get(ExtractedItem, 2)
        approved.normalized_text = "human approved page"
        approved.review_status = "approved"
        approved.needs_review = False
        db.commit()
    result = run(library, force=True)
    assert result["statistics"]["preserved_reviewed"] == 2
    assert len(library.model_calls) == 4
    with library.factory() as db:
        assert db.get(ExtractedItem, 1).normalized_text == "human edited pending page"
        assert db.get(ExtractedItem, 1).payload["human_note"] == "keep"
        assert db.get(ExtractedItem, 2).normalized_text == "human approved page"
        assert db.get(ExtractedItem, 2).review_status == "approved"
        assert db.get(ExtractedItem, 1).raw_text == "immutable original 1"
    for item in result["jobs"][0]["review_items"]:
        checkpoint = json.loads(Path(item["proposed_version"]["checkpoint"]).read_text(encoding="utf-8"))
        assert checkpoint["status"] == "success" and checkpoint["result"]["normalized_text"]


def test_force_versions_remain_latest_on_next_resume(library):
    run(library, pages=[1])
    forced = run(library, pages=[1], force=True)
    checkpoint = forced["jobs"][0]["review_items"][0]["payload"]["library_import"]["active_checkpoint"]
    resumed = run(library, pages=[1])
    assert resumed["statistics"]["resumed"] == 1 and len(library.model_calls) == 2
    assert resumed["jobs"][0]["review_items"][0]["payload"]["library_import"]["active_checkpoint"] == checkpoint


def test_corrupt_checkpoint_and_model_identity_do_not_resume(library, monkeypatch):
    first = run(library, pages=[1])
    path = first["jobs"][0]["review_items"][0]["payload"]["library_import"]["active_checkpoint"]
    Path(path).write_text("[]", encoding="utf-8")
    assert run(library, pages=[1])["statistics"]["success"] == 1
    changed = replace(library.config, model="different-model")
    monkeypatch.setattr(importer, "get_provider_config", lambda **kwargs: changed)
    assert run(library, pages=[1])["statistics"]["success"] == 1
    assert len(library.model_calls) == 3


def test_raw_text_identity_invalidates_checkpoint(library):
    run(library, pages=[1])
    with library.factory() as db:
        item = db.get(ExtractedItem, 1)
        item.raw_text = "changed externally, must not be overwritten"
        db.commit()
    assert run(library, pages=[1])["statistics"]["success"] == 1
    assert len(library.model_calls) == 2
    with library.factory() as db:
        assert db.get(ExtractedItem, 1).raw_text == "changed externally, must not be overwritten"


def test_transaction_failure_keeps_other_page_and_restores_from_checkpoint(library, monkeypatch):
    original = importer._candidate_index
    def fail_first(db, document, item, material):
        if item.page_number == 1:
            raise RuntimeError("DB failure")
        return original(db, document, item, material)
    monkeypatch.setattr(importer, "_candidate_index", fail_first)
    first = run(library)
    assert not first["completed"] and first["statistics"]["failed"] == 1 and first["statistics"]["success"] == 1
    with library.factory() as db:
        assert db.get(ExtractedItem, 1).normalized_text == "immutable original 1"
        assert len(db.get(ExtractedItem, 2).normalized_text) > 12000
    monkeypatch.setattr(importer, "_candidate_index", original)
    second = run(library)
    assert second["completed"] and second["statistics"]["resumed"] == 2
    assert len(library.model_calls) == 2


def test_empty_output_is_failure_and_is_not_success_checkpoint(library, monkeypatch):
    def empty(document, page, **kwargs):
        return {"source_page": page, "normalized_text": "", "item_type": "text", "payload": {}}
    monkeypatch.setattr(page_transcription, "transcribe_page", empty)
    result = run(library, pages=[1])
    assert result["statistics"]["failed"] == 1 and not result["completed"]
    with library.factory() as db:
        assert db.get(ExtractedItem, 1).raw_text == "immutable original 1"
        assert db.get(ExtractedItem, 1).normalized_text == "immutable original 1"
        assert db.scalar(select(Material)) is None
    monkeypatch.setattr(page_transcription, "transcribe_page", library.transcribe)
    assert run(library, pages=[1])["statistics"]["success"] == 1


def test_compatible_review_requires_explicit_approval(library):
    result = run(library, pages=[1])
    review_path = library.output / "library.review.json"
    with library.factory() as db:
        assert apply_review_manifest(review_path, db, publish=True)["jobs"][0]["published"] == 0
    result["jobs"][0]["review_items"][0]["approved"] = True
    result["jobs"][0]["review_items"][0]["quality_review_cleared"] = True
    review_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    with library.factory() as db:
        apply_review_manifest(review_path, db, publish=True)
        assert db.get(ExtractedItem, 1).review_status == "approved"
        assert db.scalar(select(Material)).verified


def test_retry_after_is_batch_only_and_semantic_failures_not_retried(library, monkeypatch):
    calls = []
    sleeps = []
    class RateLimited(RuntimeError):
        status_code = 429
        retry_after = 7.0
    def limited(document, page, **kwargs):
        calls.append(page)
        if len(calls) == 1:
            raise RateLimited()
        return library.transcribe(document, page, **kwargs)
    monkeypatch.setattr(page_transcription, "transcribe_page", limited)
    monkeypatch.setattr(importer.time, "sleep", sleeps.append)
    assert run(library, pages=[1], retries=1)["completed"]
    assert sleeps == [7.0] and calls == [1, 1]
    assert importer._retry_details(ValueError("semantic")) == (False, None)


def test_cli_returns_failed_page_exit_code_and_manifest_ids(monkeypatch, tmp_path):
    observed = {}
    monkeypatch.setattr("backend.import_library.create_tables", lambda: None)
    def invoke(specs, **kwargs):
        observed.update({"specs": specs, **kwargs})
        return {"completed": False, "statistics": {"failed": 1}, "review_manifest": "review.json"}
    monkeypatch.setattr("backend.import_library.import_library", invoke)
    assert main(["--document-ids", "2", "3", "4", "--output-dir", str(tmp_path), "--pages", "31"]) == 1
    assert observed["specs"] == [{"document_id": 2}, {"document_id": 3}, {"document_id": 4}]
    assert observed["pages"] == [31] and observed["workers"] == 2


def test_empty_actual_ocr_cannot_claim_text_transcription(library, monkeypatch):
    monkeypatch.setattr(importer, "ocr_page", lambda document, page: {"raw_text": "", "blocks": [], "confidence": 0.0})
    result = run(library, pages=[1], mode="text")
    assert not result["completed"] and result["statistics"]["failed"] == 1
    assert not library.model_calls
    with library.factory() as db:
        assert db.get(ExtractedItem, 1).raw_text == "immutable original 1"
        assert db.get(ExtractedItem, 1).review_status == "pending"


def test_changed_pdf_rejects_old_checkpoints_before_api_calls(library):
    run(library, pages=[1])
    library.pdf_path.write_bytes(b"externally replaced source")
    with pytest.raises(ValueError, match="哈希变化"):
        run(library, pages=[1])
    assert len(library.model_calls) == 1


def _replace_library_pdf(library, draw):
    with fitz.open() as pdf:
        for number in (1, 2):
            draw(pdf.new_page(), number)
        library.pdf_path.write_bytes(pdf.tobytes())
    with library.factory() as db:
        db.get(SourceDocument, 2).file_hash = sha256_path(library.pdf_path)
        db.commit()


def test_local_ocr_full_body_layout_provenance_and_resume_without_config(library, monkeypatch):
    def forbidden_config(**kwargs):
        raise AssertionError("Local OCR must not load provider configuration or credentials")
    monkeypatch.setattr(importer, "get_provider_config", forbidden_config)
    calls = []
    def box(text, x, y):
        return {"text": text, "bbox": [[x, y], [x + 100, y], [x + 100, y + 10], [x, y + 10]],
                "confidence": 0.8}
    long_body = "完整正文与损坏公式 x ? y，不补写。 " * 1500
    blocks = [box("右栏第二段", 250, 75), box("左栏第二段 " + long_body, 10, 75),
              box("右栏第一行", 250, 20), box("左栏第二行", 10, 35),
              box("左栏第一行", 10, 20), box("右栏第二行", 250, 35)]
    raw_ocr = "\n".join(block["text"] for block in blocks)
    def actual_ocr(document, page):
        calls.append(page)
        return {"raw_text": raw_ocr, "blocks": blocks, "confidence": 0.8}
    monkeypatch.setattr(importer, "ocr_page", actual_ocr)
    first = run(library, mode="ocr", provider_id="nonexistent", model="unavailable")
    assert first["completed"] and first["statistics"]["selected"] == first["statistics"]["success"] == 2
    assert not library.model_calls
    assert first["provider"]["provider"] == "local_ocr"
    with library.factory() as db:
        for number in (1, 2):
            item = db.get(ExtractedItem, number)
            assert item.raw_text == f"immutable original {number}"
            assert item.normalized_text == ("左栏第一行\n左栏第二行\n\n左栏第二段 " + long_body
                                            + "\n\n右栏第一行\n右栏第二行\n\n右栏第二段")
            assert item.payload["ocr_text"] == raw_ocr
            assert [block["text"] for block in item.payload["ocr_blocks"]] == [
                "左栏第一行", "左栏第二行", "左栏第二段 " + long_body, "右栏第一行", "右栏第二行", "右栏第二段"]
            assert [block["reading_order"] for block in item.payload["ocr_boxes"]] == list(range(6))
            assert [block["paragraph_index"] for block in item.payload["ocr_boxes"]] == [0, 0, 1, 2, 2, 3]
            assert item.payload["content_format"] == "text"
            assert item.payload["reviewed_formulas"] == item.payload["formulas"] == []
            assert "llm_provider" not in item.payload and "llm_model" not in item.payload
            diagnostics = item.payload["transcription_diagnostics"]
            assert diagnostics["mode"] == "ocr" and diagnostics["protocol"] == "local_ocr"
            assert diagnostics["human_reviewed"] is False and diagnostics["remote_provider_called"] is False
            assert item.review_status == "pending" and item.needs_review
            assert not item.payload["quality_review_cleared"]
            assert "local_ocr_requires_review" in item.payload["quality_flags"]
            material = db.scalar(select(Material).where(Material.page_number == number))
            assert material.content == item.normalized_text and not material.verified
            assert material.chunks and not material.formulas
            checkpoint = json.loads(Path(item.payload["library_import"]["active_checkpoint"]).read_text(encoding="utf-8"))
            assert checkpoint["identity"]["provider"]["base_url"] == ""
    second = run(library, mode="ocr")
    assert second["completed"] and second["statistics"]["resumed"] == 2
    assert calls == [1, 2] or calls == [2, 1]
    assert not library.model_calls


def test_local_native_parser_retains_actual_pdf_body_and_independent_ocr(library, monkeypatch):
    monkeypatch.setattr(importer, "_pdf_quality", lambda page, text: {"quality_flags": []})
    with fitz.open(library.pdf_path) as pdf:
        expected = "\n\n".join(block[4].strip() for block in pdf[0].get_text("blocks", sort=True)
                               if block[6] == 0 and block[4].strip())
    result = run(library, mode="ocr", pages=[1])
    assert result["completed"] and library.ocr_calls == [1] and not library.model_calls
    with library.factory() as db:
        item = db.get(ExtractedItem, 1)
        assert item.normalized_text == expected
        assert item.raw_text == "immutable original 1"
        assert item.payload["ocr_text"] == "actual OCR 1"
        assert item.payload["transcription_diagnostics"]["text_source"] == "native_pdf"


def test_local_verified_blank_has_evidence_no_search_chunks_and_resumes(library, monkeypatch):
    _replace_library_pdf(library, lambda page, number: None)
    calls = []
    def empty(document, page):
        calls.append(page)
        return {"raw_text": "", "blocks": [], "confidence": 0.0}
    monkeypatch.setattr(importer, "ocr_page", empty)
    result = run(library, mode="ocr")
    assert result["completed"] and not library.model_calls
    for snapshot in result["jobs"][0]["review_items"]:
        assert snapshot["item_type"] == "blank" and snapshot["normalized_text"] == ""
        assert not snapshot["approved"] and snapshot["review_status"] == "pending"
        evidence = snapshot["payload"]["blank_evidence"]
        assert evidence["is_blank"] and evidence["nonwhite_pixels"] == 0
        assert evidence["pixel_count"] == evidence["width"] * evidence["height"]
        assert evidence["minimum_channel"] == 255 and evidence["native_text_characters"] == 0
        assert len(evidence["raster_sha256"]) == 64
        assert snapshot["payload"]["blank_notice"] == "原页空白，无可检索正文；仍待人工审查"
    with library.factory() as db:
        for item in db.scalars(select(ExtractedItem)):
            assert item.raw_text == f"immutable original {item.page_number}" and item.needs_review
        for material in db.scalars(select(Material)):
            assert not material.content and not material.chunks
            assert not material.metadata_json["rag_indexed"] and not material.verified
    assert run(library, mode="ocr")["statistics"]["resumed"] == 2
    assert len(calls) == 2


@pytest.mark.parametrize("source_kind", ["native_text", "scanned_cover", "grey_border_noise", "faint_marks"])
def test_local_empty_ocr_nonblank_source_fails_with_real_render_evidence(library, monkeypatch, source_kind):
    def draw(page, number):
        if source_kind == "native_text":
            page.insert_text((50, 50), "Real source text cannot be declared blank")
        elif source_kind == "scanned_cover":
            # Raster-only page, no native text for the emptiness check.
            with fitz.open() as cover:
                image_page = cover.new_page(width=120, height=120)
                image_page.draw_rect(image_page.rect, fill=(0.3, 0.6, 0.8))
                page.insert_image(page.rect, stream=image_page.get_pixmap().tobytes("png"))
        elif source_kind == "grey_border_noise":
            page.draw_rect(fitz.Rect(0, 0, 30, page.rect.height), color=None, fill=(0.8, 0.8, 0.8))
            for y in range(50, 500, 25):
                page.draw_rect(fitz.Rect(32, y, 33, y + 1), color=None, fill=(0.5, 0.5, 0.5))
        else:
            page.draw_rect(fitz.Rect(50, 50, 120, 55), color=None, fill=(0.97, 0.97, 0.97))
    _replace_library_pdf(library, draw)
    monkeypatch.setattr(importer, "ocr_page", lambda document, page: {"raw_text": "", "blocks": [], "confidence": 0.0})
    result = run(library, mode="ocr", pages=[1])
    assert not result["completed"] and result["statistics"]["failed"] == 1
    assert not library.model_calls
    error = result["errors"][0]
    assert error["error_type"] == "LocalOCRFailure" and not error["retryable"]
    assert "实际 OCR 为空" in error["reason"]
    assert error["blank_evidence"]["is_blank"] is False
    assert error["blank_evidence"]["nonwhite_pixels"] > 0
    records = [json.loads(path.read_text(encoding="utf-8")) for path in (library.output / "checkpoints").rglob("*.error.json")]
    assert len(records) == 1 and records[0]["blank_evidence"] == error["blank_evidence"]
    with library.factory() as db:
        item = db.get(ExtractedItem, 1)
        assert item.raw_text == item.normalized_text == "immutable original 1"
        assert item.item_type == "page" and item.review_status == "pending"
        assert db.scalar(select(Material)) is None


def test_local_ocr_rejects_text_without_real_boxes(library, monkeypatch):
    monkeypatch.setattr(importer, "ocr_page", lambda document, page: {
        "raw_text": "Unlocated text", "blocks": [], "confidence": 0.8})
    result = run(library, mode="ocr", pages=[1])
    assert not result["completed"] and result["statistics"]["failed"] == 1
    assert result["errors"][0]["error_type"] == "LocalOCRFailure"
    with library.factory() as db:
        assert db.get(ExtractedItem, 1).normalized_text == "immutable original 1"
        assert db.scalar(select(Material)) is None


def test_local_force_preserves_human_state_and_keeps_proposed_ocr_version(library):
    run(library, mode="ocr")
    with library.factory() as db:
        pending = db.get(ExtractedItem, 1)
        pending.normalized_text = "Human corrected complete paragraph"
        pending.payload = {**pending.payload, "reviewed_formulas": ["human-reviewed-formula"]}
        approved = db.get(ExtractedItem, 2)
        approved.review_status = "approved"
        approved.needs_review = False
        db.commit()
    result = run(library, mode="ocr", force=True)
    assert result["statistics"]["preserved_reviewed"] == 2 and not library.model_calls
    with library.factory() as db:
        assert db.get(ExtractedItem, 1).normalized_text == "Human corrected complete paragraph"
        assert db.get(ExtractedItem, 1).payload["reviewed_formulas"] == ["human-reviewed-formula"]
        assert db.get(ExtractedItem, 2).review_status == "approved"
        assert not db.get(ExtractedItem, 2).needs_review
    for snapshot in result["jobs"][0]["review_items"]:
        record = json.loads(Path(snapshot["proposed_version"]["checkpoint"]).read_text(encoding="utf-8"))
        assert record["result"]["payload"]["reviewed_formulas"] == []
        assert record["identity"]["mode"] == "ocr" and record["ocr"]["blocks"]


@pytest.mark.parametrize("mode", ["auto", "text", "vision"])
def test_model_authentication_failure_never_silently_becomes_local_success(library, monkeypatch, mode):
    calls = []
    def authentication_error(document, page, **kwargs):
        calls.append(page)
        raise importer.AssistUnavailable("Authentication Error, All connection attempts failed", status_code=401)
    monkeypatch.setattr(page_transcription, "transcribe_page", authentication_error)
    result = run(library, mode=mode, pages=[1], retries=5)
    assert not result["completed"] and result["statistics"]["failed"] == 1
    assert calls == [1] and not result["errors"][0]["retryable"]
    assert result["errors"][0]["reason"] == "Authentication Error, All connection attempts failed"
    with library.factory() as db:
        assert db.get(ExtractedItem, 1).normalized_text == "immutable original 1"
        assert db.scalar(select(Material)) is None
    # Only an explicit new local-mode invocation is allowed to succeed.
    assert run(library, mode="ocr", pages=[1])["statistics"]["success"] == 1
    assert calls == [1]


def test_local_checkpoint_corruption_and_source_text_change_require_real_ocr_again(library):
    first = run(library, mode="ocr", pages=[1])
    checkpoint = first["jobs"][0]["review_items"][0]["payload"]["library_import"]["active_checkpoint"]
    Path(checkpoint).write_text("[]", encoding="utf-8")
    assert run(library, mode="ocr", pages=[1])["statistics"]["success"] == 1
    with library.factory() as db:
        db.get(ExtractedItem, 1).raw_text = "Externally changed original"
        db.commit()
    assert run(library, mode="ocr", pages=[1])["statistics"]["success"] == 1
    assert library.ocr_calls == [1, 1, 1] and not library.model_calls
    with library.factory() as db:
        assert db.get(ExtractedItem, 1).raw_text == "Externally changed original"


def test_local_raster_body_uses_real_ocr_and_resumes_without_recognizing_again(library, monkeypatch):
    from backend.app.services.ingestion.service import ocr_page as real_ocr_page

    def draw_scan(page, number):
        with fitz.open() as original:
            image_page = original.new_page(width=300, height=300)
            for y, text in ((45, "FULL LOCAL OCR"), (95, "COMPLETE BODY"), (145, "1234567890")):
                image_page.insert_text((20, y), text, fontsize=20)
            page.insert_image(fitz.Rect(50, 50, 450, 450),
                              stream=image_page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png"))
    _replace_library_pdf(library, draw_scan)
    calls = []
    def recognize(document, page):
        calls.append(page)
        return real_ocr_page(document, page)
    def forbidden_config(**kwargs):
        raise AssertionError("Real local OCR must not load external model configuration")
    monkeypatch.setattr(importer, "ocr_page", recognize)
    monkeypatch.setattr(importer, "get_provider_config", forbidden_config)
    result = run(library, mode="ocr", pages=[1])
    assert result["completed"] and calls == [1] and not library.model_calls
    with library.factory() as db:
        item = db.get(ExtractedItem, 1)
        assert "FULL LOCAL OCR" in item.normalized_text
        assert "COMPLETE BODY" in item.normalized_text and "1234567890" in item.normalized_text
        assert item.payload["ocr_blocks"] and item.payload["ocr_text"]
        assert all(block["bbox"] and block["confidence"] > 0 for block in item.payload["ocr_blocks"])
        assert item.payload["transcription_diagnostics"]["text_source"] == "ocr_blocks"
        assert item.raw_text == "immutable original 1" and item.review_status == "pending"
    assert run(library, mode="ocr", pages=[1])["statistics"]["resumed"] == 1
    assert calls == [1]
