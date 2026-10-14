from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event

from PIL import Image
import pytest

from backend.app import main
from backend.app.db import SessionLocal
from backend.app.models import Material, MaterialChunk, SourceDocument
from backend.app.provider_config import ProviderConfig
from backend.app.services import local_tools
from backend.app.services.answering import LLMAnswerer
from backend.app.services.retrieval import search_evidence


@pytest.fixture
def configured_model(monkeypatch):
    config = ProviderConfig(id="interactive-test", config_source="saved", enabled=True, provider="test", base_url="https://model.test/v1/responses", model="test", api_key="synthetic-only")
    monkeypatch.setattr(main, "get_provider_config", lambda **_kwargs: config)
    return config


def _reviewed_source():
    with SessionLocal() as db:
        document = SourceDocument(course_id=1, filename="study.pdf", file_hash="d" * 64, path="study.pdf", page_count=1, parser="test", status="published")
        db.add(document)
        db.flush()
        material = Material(course_id=1, source_document_id=document.id, material_type="text", title="采样定理", section="采样定理", content="采样频率应高于信号最高频率的两倍，避免抽样后频谱混叠。" * 8, source_anchor=f"doc:{document.id}:p1", verified=True)
        db.add(material)
        db.commit()
        return material.id


@pytest.mark.parametrize("kind", ["duplicate", "excluded", "foreign_evidence", "incomplete"])
def test_suggestion_generation_rejects_unusable_model_questions(client, configured_model, monkeypatch, kind):
    material_id = _reviewed_source()
    questions = ["采样定理的条件是什么？", "怎样观察抽样造成的混叠？", "如何绘制一个信号的频谱？"]
    result = {"suggestions": questions, "evidence_ids": [material_id]}
    if kind == "duplicate":
        result["suggestions"] = [questions[0], questions[0], questions[2]]
    elif kind == "foreign_evidence":
        result["evidence_ids"] = [999999]
    elif kind == "incomplete":
        result["suggestions"] = questions[:2]

    async def completion(self, *_args):
        return result

    monkeypatch.setattr(LLMAnswerer, "request_json", completion)
    response = client.post("/api/study-suggestions", json={"course_id": 1, "exclude": [questions[0]] if kind == "excluded" else []})
    assert response.status_code == 503
    assert "suggestions" not in response.json()


def test_unreviewed_scope_cannot_generate_learning_questions(client, configured_model, monkeypatch):
    material_id = _reviewed_source()
    with SessionLocal() as db:
        db.get(Material, material_id).verified = False
        db.commit()

    async def must_not_generate(self, *_args):
        raise AssertionError("Unreviewed material cannot be supplied to model")

    monkeypatch.setattr(LLMAnswerer, "request_json", must_not_generate)
    response = client.post("/api/study-suggestions", json={"course_id": 1})
    assert response.status_code == 503


def test_real_local_convolution_and_generated_artifact_survive_answer_replay(client, configured_model, monkeypatch, tmp_path):
    monkeypatch.setattr(local_tools, "ARTIFACT_DIR", tmp_path / "artifacts")

    async def model(self, _question, _evidence, _web=None, history=None, *, tool_results=None, generate_title=False):
        if tool_results is None:
            return {"answer": "执行本地离散卷积。", "evidence_ids": [], "retrieval_queries": ["线性卷积 离散序列"], "tool_requests": [{"tool": "convolution", "arguments": {"x": [1, 2], "h": [1, -1], "x_start": -1, "h_start": 2}}]}
        return {"answer": "本地离散卷积在索引1、2、3的结果为1、1、-2。", "evidence_ids": [], "tool_requests": [], "conversation_title": "离散序列卷积计算" if generate_title else None}

    monkeypatch.setattr(LLMAnswerer, "complete", model)
    response = client.post("/api/answers/ask", json={"question": "计算两条给定离散序列的卷积并画图"})
    assert response.status_code == 200
    answer = response.json()
    assert answer["conversation_title"] == "离散序列卷积计算"
    assert answer["conversation_title_source"] == "model"
    result = answer["tool_results"][0]
    assert result["status"] == "completed"
    assert result["data"]["indices"] == [1, 2, 3]
    assert result["data"]["values"] == [1, 1, -2]
    replay = client.get(f"/api/answers/{answer['answer_id']}").json()
    assert replay["tool_results"] == answer["tool_results"]
    history = client.get(f"/api/conversations/{answer['conversation_id']}").json()
    assert history["turns"][0]["tool_results"] == answer["tool_results"]
    image = client.get(replay["tool_results"][0]["artifacts"][0]["url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    with Image.open(BytesIO(image.content)) as plot:
        assert plot.format == "PNG" and plot.width >= 600 and plot.height >= 400
    assert client.delete(f"/api/conversations/{answer['conversation_id']}").status_code == 204
    assert client.get(replay["tool_results"][0]["artifacts"][0]["url"]).status_code == 404
    assert client.get(f"/api/answers/{answer['answer_id']}").status_code == 404


def test_deletion_during_final_tool_explanation_discards_answer_and_generated_png(client, configured_model, monkeypatch, tmp_path):
    monkeypatch.setattr(local_tools, "ARTIFACT_DIR", tmp_path / "artifacts")
    entered, release = Event(), Event()
    artifact_urls = []

    async def model(self, _question, _evidence, _web=None, history=None, *, tool_results=None, generate_title=False):
        if tool_results is None:
            return {"answer": "", "evidence_ids": [], "tool_requests": [{"tool": "convolution", "arguments": {"x": [1, 2], "h": [1, -1]}}]}
        artifact_urls.extend(artifact["url"] for result in tool_results for artifact in result["artifacts"])
        entered.set()
        assert await asyncio.to_thread(release.wait, 10), "test did not release final explanation"
        return {"answer": "离散卷积结果为1、1、-2。", "evidence_ids": [], "tool_requests": [], "conversation_title": "离散序列卷积计算"}

    monkeypatch.setattr(LLMAnswerer, "complete", model)
    conversation = client.post("/api/conversations", json={}).json()
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.post, "/api/answers/ask", json={"conversation_id": conversation["id"], "question": "计算离散卷积并画图"})
        try:
            assert entered.wait(15)
            assert artifact_urls
            assert all(client.get(url).status_code == 200 for url in artifact_urls)
            assert client.delete(f"/api/conversations/{conversation['id']}").status_code == 204
        finally:
            release.set()
        response = request.result(timeout=15)
    assert response.status_code == 404
    assert client.get(f"/api/conversations/{conversation['id']}").status_code == 404
    assert client.get("/api/conversations").json() == []
    assert all(client.get(url).status_code == 404 for url in artifact_urls)


def test_colloquial_question_recalls_textbook_terms_without_leaving_selected_book(client, configured_model, monkeypatch):
    question = "为什么采样之后会出现频谱重叠？"
    with SessionLocal() as db:
        book = SourceDocument(course_id=1, filename="aliases.pdf", file_hash="e" * 64, path="aliases.pdf", page_count=1, parser="test", status="published")
        db.add(book)
        db.flush()
        material = Material(course_id=1, source_document_id=book.id, material_type="text", title="奈奎斯特准则", content="信号带宽需低于抽取速率的一半，否则混叠。", source_anchor=f"doc:{book.id}:p1", page_number=1, verified=True)
        foreign = Material(course_id=1, material_type="text", title="混叠 奈奎斯特条件", content="混叠 奈奎斯特条件。" * 20, source_anchor="other-book", verified=True)
        revoked = Material(course_id=1, source_document_id=book.id, material_type="text", title="混叠 奈奎斯特条件", content="混叠 奈奎斯特条件。" * 20, source_anchor="revoked", verified=True, metadata_json={"publication_status": "revoked"})
        db.add_all([material, foreign, revoked])
        db.flush()
        for item in (material, foreign, revoked):
            db.add(MaterialChunk(material_id=item.id, chunk_index=0, text=item.content))
        db.commit()
        book_id, material_id = book.id, material.id
        assert search_evidence(db, question, source_document_id=book_id) == []

    replies = iter([
        {"answer": "", "evidence_ids": [], "retrieval_queries": ["混叠 奈奎斯特条件"], "tool_requests": []},
        {"answer": "教材给出：信号带宽需低于抽取速率的一半，否则会发生混叠。", "evidence_ids": [material_id], "retrieval_queries": [], "tool_requests": [], "conversation_title": "抽样混叠与奈奎斯特条件"},
    ])

    async def model(self, *_args):
        return next(replies)

    monkeypatch.setattr(LLMAnswerer, "request_json", model)
    response = client.post("/api/answers/ask", json={"question": question, "course_id": 1, "source_document_id": book_id})
    assert response.status_code == 200
    answer = response.json()
    assert [item["material_id"] for item in answer["evidence"]] == [material_id]
    assert answer["evidence"][0]["source_anchor"] == f"doc:{book_id}:p1"
    assert answer["question"] == question
    assert answer["conversation_title"] == "抽样混叠与奈奎斯特条件"
    assert next(step for step in answer["agent_trace"] if step["step"] == "answer_generation")["cited_evidence_ids"] == [material_id]
    assert client.get(f"/api/answers/{answer['answer_id']}").json()["evidence"] == answer["evidence"]
