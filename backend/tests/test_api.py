from io import BytesIO

import fitz
from PIL import Image

from backend.app.db import SessionLocal
from backend.app.models import AnswerRecord, EvidenceLink, ExtractedItem, IngestionJob, Material, MaterialChunk, SourceDocument

def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"
    assert "env_path" not in response.json()
    assert "api_key" not in response.json()


def test_frontend_origin_is_allowed_for_api(client):
    response = client.options(
        "/api/admin/providers",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-admin-token",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"

def test_validate_returns_traceable_checks(client):
    response = client.post("/api/answers/validate", json={"question": "采样频率为8 kHz时最高可表示频率是多少？", "answer": "fmax = fs / 2，结果为8 kHz，频率6000 Hz。"})
    assert response.status_code == 200
    body = response.json()
    statuses = {item["category"]: item["status"] for item in body["checks"]}
    assert statuses["formula"] == "pass"
    assert statuses["numeric"] == "fail"
    assert statuses["cross_modal"] == "conflict"
    assert body["traceability"]["status"] == "traced"
    assert body["risk_flags"]

def test_validate_missing_unit_is_public_failure_and_history_is_readable(client):
    response = client.post(
        "/api/answers/validate",
        json={"question": "采样频率为8 kHz时最高可表示频率是多少？", "answer": "结果为4"},
    )
    assert response.status_code == 200
    body = response.json()
    numeric = next(item for item in body["checks"] if item["category"] == "numeric")
    assert numeric["status"] == "fail"
    assert numeric["details"]["reason"] == "missing_unit"

    history = client.get(f"/api/answers/{body['answer_id']}")
    assert history.status_code == 200
    history_numeric = next(item for item in history.json()["checks"] if item["category"] == "numeric")
    assert history_numeric["status"] == "fail"

def test_model_failure_is_explicit_and_never_claims_model_citations(client, monkeypatch):
    from backend.app.services.answering import LLMAnswerer

    async def unavailable(self, question, evidence, web_results=None, history=None, *, generate_title=False):
        self.last_error = "not_configured"
        return None

    monkeypatch.setattr(LLMAnswerer, "complete", unavailable)
    response = client.post("/api/answers/ask", json={"question": "解释奈奎斯特采样定理"})
    assert response.status_code == 200
    body = response.json()
    generation = next(item for item in body["agent_trace"] if item["step"] == "answer_generation")
    assert generation["status"] == "fallback"
    assert generation["fallback_reason"] == "not_configured"
    assert generation["cited_evidence_ids"] == []
    history = client.get(f"/api/answers/{body['answer_id']}").json()
    assert history["agent_trace"][0]["status"] == "fallback"
    assert body["evidence"]


def test_unknown_material_id_is_rejected(client):
    response = client.post("/api/answers/validate", json={"question": "问题", "answer": "回答", "material_ids": [999999]})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unknown_material_ids"


def test_metrics_have_case_coverage(client):
    response = client.get("/api/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["course_count"] >= 3
    assert body["topic_count"] >= 20
    assert body["evaluation_case_count"] >= 100


def test_build_rag_indexes_every_book_page_and_scopes_retrieval(client):
    document = fitz.open()
    for text in (
        "采样定理：为了避免混叠，采样频率必须至少是信号最高频率的两倍。公式 fs >= 2 fmax。",
        "离散时间信号只在离散时刻定义，离散系统可以用序列与单位脉冲响应描述。",
        "数字信号处理中的频谱分析用于观察信号在频率域的分布，并区分幅度谱和相位谱。",
    ):
        page = document.new_page()
        page.insert_textbox((30, 30, 560, 760), text)
    buffer = BytesIO()
    document.save(buffer)
    document.close()

    uploaded = client.post(
        "/api/ingestion/upload?course_id=1",
        files={"file": ("complete-rag-book.pdf", buffer.getvalue(), "application/pdf")},
    )
    assert uploaded.status_code == 200
    job_id = uploaded.json()["id"]
    source_document_id = uploaded.json()["source_document_id"]

    built = client.post(f"/api/ingestion/jobs/{job_id}/build-rag?course_id=1")
    assert built.status_code == 200
    assert built.json()["indexed_pages"] == 3
    assert built.json()["coverage"] == 1.0
    assert built.json()["candidate_pages"] == 3
    assert built.json()["created"] == 3

    status = client.get(f"/api/ingestion/jobs/{job_id}/rag")
    assert status.status_code == 200
    assert status.json()["indexed_pages"] == 3
    assert status.json()["material_count"] == 3
    assert status.json()["chunk_count"] == 3

    documents = client.get("/api/source-documents?course_id=1")
    assert documents.status_code == 200
    current = next(item for item in documents.json() if item["id"] == source_document_id)
    assert current["rag_coverage"] == 1.0
    assert current["rag_candidate_pages"] == 3

    search = client.post(
        "/api/search",
        json={"query": "fs fmax", "source_document_id": source_document_id, "limit": 10},
    )
    assert search.status_code == 200
    results = search.json()["results"]
    assert results and all(item["source_anchor"].startswith(f"doc:{source_document_id}:") for item in results)
    assert all(item["verified"] is False for item in results)

    repeated = client.post(f"/api/ingestion/jobs/{job_id}/build-rag?course_id=1")
    assert repeated.status_code == 200
    assert repeated.json()["created"] == 0
    assert repeated.json()["unchanged"] == 3
    job_items = client.get(f"/api/ingestion/jobs/{job_id}").json()["items"]
    first_item = job_items[0]
    approved = client.patch(
        f"/api/ingestion/items/{first_item['id']}",
        json={"review_status": "approved"},
    )
    assert approved.status_code == 200
    published = client.post(f"/api/ingestion/jobs/{job_id}/publish?course_id=1")
    assert published.status_code == 200
    rag_after_publish = client.get(f"/api/ingestion/jobs/{job_id}/rag")
    assert rag_after_publish.status_code == 200
    assert rag_after_publish.json()["verified_pages"] == 1
    assert rag_after_publish.json()["indexed_pages"] == 3

def test_quality_review_cannot_be_bypassed_by_omitting_payload_flags(client):
    db = SessionLocal()
    try:
        document = SourceDocument(course_id=1, filename="scanned.pdf", file_hash="a" * 64, path="scanned.pdf", page_count=1, parser="pymupdf", status="review")
        db.add(document)
        db.flush()
        job = IngestionJob(source_document_id=document.id, status="review", processed_pages=1, total_pages=1)
        db.add(job)
        db.flush()
        item = ExtractedItem(
            ingestion_job_id=job.id,
            page_number=1,
            item_type="text",
            raw_text="原生文本",
            normalized_text="原生文本",
            payload={"quality_flags": ["full_page_image_native_text"], "text_quality": "needs_review"},
            confidence=0.35,
            needs_review=True,
            review_status="pending",
            source_anchor=f"doc:{document.id}:p1",
        )
        db.add(item)
        db.flush()
        material = Material(
            course_id=1,
            topic_id=1,
            source_document_id=document.id,
            material_type="text",
            title="待撤销证据",
            content="原生文本",
            page_number=1,
            section="待审核",
            source_anchor=f"doc:{document.id}:p1",
            metadata_json={},
            verified=True,
        )
        db.add(material)
        db.flush()
        db.add(MaterialChunk(material_id=material.id, chunk_index=0, text="原生文本", keywords=[]))
        db.commit()
        item_id = item.id
        material_id = material.id
        job_id = job.id

        rejected_approval = client.patch(
            f"/api/ingestion/items/{item_id}",
            json={"normalized_text": "修订后的教材文本", "review_status": "approved"},
        )
        assert rejected_approval.status_code == 422
        db.refresh(item)
        assert item.payload["quality_flags"] == ["full_page_image_native_text"]

        blocked = client.post(f"/api/ingestion/jobs/{job_id}/publish?course_id=1")
        assert blocked.status_code == 200
        assert blocked.json()["skipped"] == 1
        db.expire_all()
        material = db.get(Material, material_id)
        assert material is not None
        assert material.verified is False

        approved = client.patch(
            f"/api/ingestion/items/{item_id}",
            json={
                "normalized_text": "修订后的教材文本",
                "review_status": "approved",
                "quality_review_cleared": True,
            },
        )
        assert approved.status_code == 200
        assert approved.json()["payload"]["quality_review_cleared"] is True
        assert approved.json()["needs_review"] is False
        assert approved.json()["confidence"] == 0.95

        published = client.post(f"/api/ingestion/jobs/{job_id}/publish?course_id=1")
        assert published.status_code == 200
        assert published.json()["updated"] == 1
        db.expire_all()
        material = db.get(Material, material_id)
        assert material is not None
        assert material.verified is True
        assert material.metadata_json["publication_status"] == "published"
    finally:
        db.close()



def test_legacy_pdf_quality_audit_is_idempotent_and_revokes_old_material(client, tmp_path):
    pdf_path = tmp_path / "legacy.pdf"
    image_buffer = BytesIO()
    Image.new("RGB", (32, 32), "white").save(image_buffer, format="PNG")
    document_pdf = fitz.open()
    page = document_pdf.new_page(width=200, height=300)
    page.insert_image(page.rect, stream=image_buffer.getvalue())
    page.insert_text((20, 40), "legacy native text layer requiring a fresh quality audit")
    document_pdf.save(pdf_path)
    document_pdf.close()

    db = SessionLocal()
    try:
        document = SourceDocument(course_id=1, filename="legacy.pdf", file_hash="c" * 64, path=str(pdf_path), page_count=1, parser="pymupdf", status="published")
        db.add(document)
        db.flush()
        job = IngestionJob(source_document_id=document.id, status="published", processed_pages=1, total_pages=1)
        db.add(job)
        db.flush()
        item = ExtractedItem(
            ingestion_job_id=job.id,
            page_number=1,
            item_type="text",
            raw_text="legacy native text layer requiring a fresh quality audit",
            normalized_text="legacy native text layer requiring a fresh quality audit",
            payload={"section": "遗留导入"},
            confidence=0.72,
            needs_review=False,
            review_status="approved",
            source_anchor=f"doc:{document.id}:p1",
        )
        db.add(item)
        db.flush()
        material = Material(
            course_id=1,
            topic_id=1,
            source_document_id=document.id,
            material_type="text",
            title="遗留资料",
            content=item.normalized_text,
            page_number=1,
            section="遗留导入",
            source_anchor=item.source_anchor,
            metadata_json={"publication_status": "published"},
            verified=True,
        )
        db.add(material)
        db.commit()
        job_id, item_id, material_id = job.id, item.id, material.id
    finally:
        db.close()

    first = client.post(f"/api/ingestion/jobs/{job_id}/quality-audit")
    assert first.status_code == 200
    assert first.json() == {"audited_pages": 1, "flagged": 1, "revoked": 1, "status": "review"}

    db = SessionLocal()
    try:
        item = db.get(ExtractedItem, item_id)
        material = db.get(Material, material_id)
        assert item is not None and item.review_status == "pending" and item.needs_review is True
        assert "full_page_image_native_text" in item.payload["quality_flags"]
        assert material is not None and material.verified is False
        assert material.metadata_json["publication_status"] == "revoked"
    finally:
        db.close()

    second = client.post(f"/api/ingestion/jobs/{job_id}/quality-audit")
    assert second.status_code == 200
    assert second.json() == {"audited_pages": 1, "flagged": 1, "revoked": 0, "status": "review"}


def test_re_review_republishes_in_place_and_clears_stale_chunk_links(client):
    db = SessionLocal()
    try:
        document = SourceDocument(course_id=1, filename="repeat.pdf", file_hash="b" * 64, path="repeat.pdf", page_count=1, parser="markdown", status="review")
        db.add(document)
        db.flush()
        job = IngestionJob(source_document_id=document.id, status="review", processed_pages=1, total_pages=1)
        db.add(job)
        db.flush()
        item = ExtractedItem(
            ingestion_job_id=job.id,
            page_number=1,
            item_type="text",
            raw_text="旧教材文本",
            normalized_text="旧教材文本",
            payload={},
            confidence=0.9,
            needs_review=False,
            review_status="approved",
            source_anchor=f"doc:{document.id}:p1",
        )
        db.add(item)
        db.flush()
        material = Material(
            course_id=1,
            topic_id=1,
            source_document_id=document.id,
            material_type="text",
            title="重复发布测试",
            content="旧教材文本",
            page_number=1,
            section="测试",
            source_anchor=item.source_anchor,
            metadata_json={"publication_status": "published"},
            verified=True,
        )
        db.add(material)
        db.flush()
        first_chunk = MaterialChunk(material_id=material.id, chunk_index=0, text="旧教材文本", keywords=[])
        stale_chunk = MaterialChunk(material_id=material.id, chunk_index=1, text="旧资料残留", keywords=[])
        db.add_all([first_chunk, stale_chunk])
        db.flush()
        answer = AnswerRecord(question="测试", answer="测试", result_json={})
        db.add(answer)
        db.flush()
        link = EvidenceLink(answer_id=answer.id, claim="旧证据", material_id=material.id, chunk_id=stale_chunk.id)
        db.add(link)
        db.commit()
        item_id, job_id, material_id, link_id = item.id, job.id, material.id, link.id
    finally:
        db.close()

    reviewed = client.patch(
        f"/api/ingestion/items/{item_id}",
        json={"normalized_text": "新教材文本", "review_status": "approved"},
    )
    assert reviewed.status_code == 200
    first_publish = client.post(f"/api/ingestion/jobs/{job_id}/publish?course_id=1")
    assert first_publish.status_code == 200
    assert first_publish.json()["updated"] == 1

    db = SessionLocal()
    try:
        material = db.get(Material, material_id)
        link = db.get(EvidenceLink, link_id)
        assert material is not None and material.verified is True
        assert material.metadata_json["publication_status"] == "published"
        assert len(material.chunks) == 1
        assert link is not None and link.chunk_id is None
    finally:
        db.close()

    second_publish = client.post(f"/api/ingestion/jobs/{job_id}/publish?course_id=1")
    assert second_publish.status_code == 200
    assert second_publish.json()["skipped"] == 1


def test_search_aggregates_chunks_and_hides_revoked_materials(client):
    db = SessionLocal()
    try:
        aggregate = Material(
            course_id=1,
            topic_id=1,
            material_type="text",
            title="rare-aggregation-marker",
            content="rare-aggregation-marker",
            source_anchor="test:aggregation",
            metadata_json={},
            verified=True,
        )
        duplicate = Material(
            course_id=1,
            topic_id=1,
            material_type="text",
            title="rare-aggregation-marker duplicate",
            content="rare-aggregation-marker duplicate",
            source_anchor="test:aggregation",
            metadata_json={},
            verified=False,
        )
        revoked = Material(
            course_id=1,
            topic_id=1,
            material_type="text",
            title="rare-revoked-marker",
            content="rare-revoked-marker",
            source_anchor="test:revoked",
            metadata_json={"publication_status": "revoked"},
            verified=False,
        )
        db.add_all([aggregate, duplicate, revoked])
        db.flush()
        aggregate_first = MaterialChunk(material_id=aggregate.id, chunk_index=0, text="rare-aggregation-marker first", keywords=[])
        aggregate_second = MaterialChunk(material_id=aggregate.id, chunk_index=1, text="rare-aggregation-marker second", keywords=[])
        duplicate_chunk = MaterialChunk(material_id=duplicate.id, chunk_index=0, text="rare-aggregation-marker duplicate", keywords=[])
        revoked_chunk = MaterialChunk(material_id=revoked.id, chunk_index=0, text="rare-revoked-marker", keywords=[])
        db.add_all([aggregate_first, aggregate_second, duplicate_chunk, revoked_chunk])
        db.commit()
        aggregate_id = aggregate.id
        aggregate_first_id = aggregate_first.id
        duplicate_id = duplicate.id
        revoked_id = revoked.id
    finally:
        db.close()

    response = client.post("/api/search", json={"query": "rare-aggregation-marker rare-revoked-marker", "limit": 50})
    assert response.status_code == 200
    results = response.json()["results"]
    result_ids = [result["material_id"] for result in results]
    assert result_ids.count(aggregate_id) == 1
    aggregate_result = next(result for result in results if result["source_anchor"] == "test:aggregation")
    assert aggregate_result["chunk_id"] == aggregate_first_id
    assert aggregate_result["snippet"].startswith("rare-aggregation-marker first")
    assert duplicate_id not in result_ids
    assert revoked_id not in result_ids
