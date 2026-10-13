from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import re
from threading import Event

import httpx
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from backend.app import db, main
from backend.app.db import SessionLocal
from backend.app.models import AnswerRecord, Conversation, ConversationTurn, Material, MaterialChunk, SourceDocument
from backend.app.provider_config import ProviderConfig
from backend.app.services import answering


def _model_transport(monkeypatch, *, entered=None, release=None):
    calls = []
    config = ProviderConfig(id="regression", config_source="environment", enabled=True, provider="regression", base_url="https://model.test/v1/responses", model="fixed-answer", api_key="test-only", gateway_messages=True)
    monkeypatch.setattr(main, "get_provider_config", lambda **_kwargs: config)

    class Transport:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, **kwargs):
            payload = kwargs["json"]
            calls.append(payload)
            if entered is not None:
                entered.set()
                assert await asyncio.to_thread(release.wait, 10), "test did not release model request"
            current_evidence = payload["input"].split("教材证据：\n", 1)[1]
            match = re.search(r"\[教材证据 (\d+)\]", current_evidence)
            result = {"answer": "教材指出采样频率应大于信号最高频率的两倍，以避免混叠。", "evidence_ids": [int(match.group(1))] if match else []}
            return httpx.Response(200, json={"output_text": json.dumps(result, ensure_ascii=False)}, request=httpx.Request("POST", url))

    monkeypatch.setattr(answering.httpx, "AsyncClient", Transport)
    return calls


def _document(course_id, marker):
    with SessionLocal() as session:
        document = SourceDocument(course_id=course_id, filename=f"{marker}.pdf", file_hash=marker.ljust(64, "0"), path=f"{marker}.pdf", page_count=1, parser="test", status="published")
        session.add(document)
        session.flush()
        material = Material(course_id=course_id, source_document_id=document.id, material_type="text", title=f"{marker} 采样定理", content=f"{marker} 采样定理指出为了避免混叠，采样频率应大于信号最高频率的两倍。", page_number=1, source_anchor=f"doc:{document.id}:p1", verified=True)
        session.add(material)
        session.flush()
        session.add(MaterialChunk(material_id=material.id, chunk_index=0, text=material.content, keywords=["采样", marker]))
        session.commit()
        return document.id


def test_conversation_isolation_history_prompt_and_exact_restoration(client, monkeypatch):
    calls = _model_transport(monkeypatch)
    doc_a, doc_b = _document(1, "alpha"), _document(2, "beta")
    a = client.post("/api/conversations", json={"course_id": 1, "source_document_id": doc_a}).json()
    b = client.post("/api/conversations", json={"course_id": 2, "source_document_id": doc_b}).json()
    assert a["id"] != b["id"]
    assert client.get(f"/api/conversations/{b['id']}").json()["turns"] == []
    first = client.post("/api/answers/ask", json={"conversation_id": a["id"], "question": "alpha 采样定理是什么？"})
    assert first.status_code == 200
    first = first.json()
    assert first["conversation_id"] == a["id"]
    assert first["agent_trace"][0]["status"] == "model"
    follow = client.post("/api/answers/ask", json={"conversation_id": a["id"], "question": "为什么需要这样？"})
    assert follow.status_code == 200
    assert follow.json()["agent_trace"][0]["status"] == "model"
    assert follow.json()["agent_trace"][0]["rag_source_anchors"] == [f"doc:{doc_a}:p1"]
    assert follow.json()["agent_trace"][0]["cited_evidence_ids"]
    assert "alpha 采样定理是什么？" in calls[-1]["input"]
    assert first["answer"] in calls[-1]["input"]
    second = client.post("/api/answers/ask", json={"conversation_id": b["id"], "question": "beta 采样定理是什么？"}).json()
    assert "alpha" not in calls[-1]["input"]
    assert all(item["source_anchor"] == f"doc:{doc_b}:p1" for item in second["evidence"])
    restored = client.get(f"/api/conversations/{a['id']}").json()
    assert restored["title"] == first["question"]
    assert restored["course_id"] == 1 and restored["source_document_id"] == doc_a
    assert restored["turns"] == [first, follow.json()]
    assert client.get(f"/api/answers/{first['answer_id']}").json() == first
    listed = client.get("/api/conversations").json()
    assert {item["id"] for item in listed} == {a["id"], b["id"]}
    assert all(item["created_at"] and item["updated_at"] for item in listed)


def test_new_conversation_remains_empty_while_old_request_finishes(client, monkeypatch):
    entered, release = Event(), Event()
    _model_transport(monkeypatch, entered=entered, release=release)
    a = client.post("/api/conversations", json={}).json()
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.post, "/api/answers/ask", json={"conversation_id": a["id"], "question": "解释奈奎斯特采样定理"})
        try:
            assert entered.wait(10)
            b = client.post("/api/conversations", json={}).json()
            assert client.get(f"/api/conversations/{b['id']}").json()["turns"] == []
        finally:
            release.set()
        response = request.result(timeout=15)
    assert response.status_code == 200
    assert response.json()["conversation_id"] == a["id"]
    assert client.get(f"/api/conversations/{a['id']}").json()["turns"] == [response.json()]
    assert client.get(f"/api/conversations/{b['id']}").json()["turns"] == []


def test_unknown_conversation_and_omitted_id_never_attach_existing(client, monkeypatch):
    _model_transport(monkeypatch)
    assert client.get("/api/conversations/999999").status_code == 404
    assert client.post("/api/answers/ask", json={"conversation_id": 999999, "question": "采样定理"}).status_code == 404
    existing = client.post("/api/conversations", json={}).json()
    first = client.post("/api/answers/ask", json={"question": "解释采样定理"}).json()
    second = client.post("/api/answers/ask", json={"question": "解释采样定理"}).json()
    assert len({existing["id"], first["conversation_id"], second["conversation_id"]}) == 3
    assert client.get(f"/api/conversations/{existing['id']}").json()["turns"] == []


def test_scope_changes_and_validate_only_orphans_are_preserved(client, monkeypatch):
    calls = _model_transport(monkeypatch)
    orphan = client.post("/api/answers/validate", json={"question": "采样频率为8 kHz时最高可表示频率是多少？", "answer": "最高频率为4 kHz。"}).json()
    assert orphan["conversation_id"] is None
    a = client.post("/api/conversations", json={"course_id": 1}).json()
    doc_b = _document(2, "scopechange")
    assert client.post("/api/conversations", json={"course_id": 1, "source_document_id": doc_b}).status_code == 400
    asked = client.post("/api/answers/ask", json={"conversation_id": a["id"], "course_id": 2, "topic_id": None, "source_document_id": doc_b, "question": "scopechange 采样定理"})
    assert asked.status_code == 200
    scoped = client.get(f"/api/conversations/{a['id']}").json()
    assert scoped["course_id"] == 2 and scoped["source_document_id"] == doc_b
    assert "历史对话（非教材证据）" not in calls[-1]["input"]
    db.create_tables()
    assert client.get(f"/api/answers/{orphan['answer_id']}").json() == orphan
    with SessionLocal() as session:
        assert session.scalar(select(ConversationTurn).where(ConversationTurn.answer_id == orphan["answer_id"])) is None


def test_table_creation_is_additive_for_legacy_answer_records(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    try:
        AnswerRecord.__table__.create(engine)
        with Session(engine) as session:
            record = AnswerRecord(question="旧问题", answer="旧回答", result_json={"agent_trace": [{"step": "legacy"}]})
            session.add(record)
            session.commit()
            record_id = record.id
        original_columns = [(column["name"], str(column["type"]), column["nullable"]) for column in inspect(engine).get_columns("answer_records")]
        monkeypatch.setattr(db, "engine", engine)
        db.create_tables()
        assert [(column["name"], str(column["type"]), column["nullable"]) for column in inspect(engine).get_columns("answer_records")] == original_columns
        assert {Conversation.__tablename__, ConversationTurn.__tablename__}.issubset(inspect(engine).get_table_names())
        with Session(engine) as session:
            preserved = session.get(AnswerRecord, record_id)
            assert preserved.answer == "旧回答"
            assert preserved.result_json == {"agent_trace": [{"step": "legacy"}]}
    finally:
        engine.dispose()
