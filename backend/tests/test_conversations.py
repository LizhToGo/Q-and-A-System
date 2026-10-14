from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import re
from threading import Event

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from backend.app import db, main
from backend.app.db import SessionLocal
from backend.app.models import AnswerRecord, Conversation, ConversationTurn, EvidenceLink, Material, MaterialChunk, SourceDocument, ValidationResultRecord
from backend.app.provider_config import ProviderConfig
from backend.app.services import answering, local_tools


def _model_transport(monkeypatch, *, entered=None, release=None, title="采样定理与混叠条件"):
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
            if payload["input"].startswith("{"):
                inputs = json.loads(payload["input"])["conversations"]
                result = {"titles": [{"conversation_id": item["conversation_id"], "title": title} for item in inputs]}
            else:
                current_evidence = payload["input"].split("教材证据：\n", 1)[1]
                match = re.search(r"\[教材证据 (\d+)\]", current_evidence)
                result = {"answer": "教材指出采样频率应大于信号最高频率的两倍，以避免混叠。", "evidence_ids": [int(match.group(1))] if match else [], "conversation_title": title}
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
    assert first["conversation_title"] == "采样定理与混叠条件"
    assert first["conversation_title_source"] == "model"
    assert len(calls) == 1
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
    assert restored["title"] == first["conversation_title"]
    assert restored["title_source"] == "model"
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
    listed_ids = {item["id"] for item in client.get("/api/conversations").json()}
    assert listed_ids == {first["conversation_id"], second["conversation_id"]}
    assert existing["id"] not in listed_ids


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
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title VARCHAR(200) NOT NULL, course_id INTEGER, topic_id INTEGER, source_document_id INTEGER, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"))
            connection.execute(text("INSERT INTO conversations (id, title, created_at, updated_at) VALUES (7, '旧问题长前缀标题', '2020-01-02 03:04:05.000000', '2020-01-02 03:04:06.000000')"))
        ConversationTurn.__table__.create(engine)
        with Session(engine) as session:
            session.add(ConversationTurn(conversation_id=7, answer_id=record_id))
            session.commit()
        original_columns = [(column["name"], str(column["type"]), column["nullable"]) for column in inspect(engine).get_columns("answer_records")]
        monkeypatch.setattr(db, "engine", engine)
        db.create_tables()
        db.create_tables()  # A second startup must not duplicate the migration.
        assert [(column["name"], str(column["type"]), column["nullable"]) for column in inspect(engine).get_columns("answer_records")] == original_columns
        assert {Conversation.__tablename__, ConversationTurn.__tablename__}.issubset(inspect(engine).get_table_names())
        with Session(engine) as session:
            preserved = session.get(AnswerRecord, record_id)
            assert preserved.answer == "旧回答"
            assert preserved.result_json == {"agent_trace": [{"step": "legacy"}]}
            conversation = session.get(Conversation, 7)
            assert conversation.title == "旧问题长前缀标题"
            assert conversation.title_source == "pending"
            assert conversation.updated_at.isoformat() == "2020-01-02T03:04:06"
            assert session.scalar(select(ConversationTurn.answer_id).where(ConversationTurn.conversation_id == 7)) == record_id
    finally:
        engine.dispose()


@pytest.mark.parametrize("title", [None, "", "$采样定理$", "过长" * 17, {"text": "采样定理"}])
def test_missing_or_invalid_title_preserves_valid_model_answer(client, monkeypatch, title):
    calls = _model_transport(monkeypatch, title=title)
    response = client.post("/api/answers/ask", json={"question": "解释采样定理"})
    assert response.status_code == 200
    answer = response.json()
    assert len(calls) == 1
    assert answer["agent_trace"][0]["status"] == "model"
    assert answer["conversation_title_source"] == "pending"
    assert client.get(f"/api/answers/{answer['answer_id']}").json() == answer


def test_custom_title_and_later_turns_are_never_renamed(client, monkeypatch):
    _model_transport(monkeypatch)
    custom = client.post("/api/conversations", json={"title": "  我的采样专题  "}).json()
    assert custom["title_source"] == "user" and custom["title"] == "我的采样专题"
    answer = client.post("/api/answers/ask", json={"conversation_id": custom["id"], "question": "解释采样定理"}).json()
    assert answer["conversation_title"] == custom["title"] and answer["conversation_title_source"] == "user"
    generated = client.post("/api/answers/ask", json={"question": "采样定理"}).json()
    _model_transport(monkeypatch, title="模型企图替换主题")
    follow = client.post("/api/answers/ask", json={"conversation_id": generated["conversation_id"], "question": "继续解释"}).json()
    assert follow["conversation_title"] == generated["conversation_title"]
    assert follow["conversation_title_source"] == "model"
    assert client.post("/api/conversations", json={"title": "   "}).status_code == 422


def test_batch_titles_use_first_qa_without_reordering_or_mutating_answers(client, monkeypatch):
    _model_transport(monkeypatch, title=None)
    first = client.post("/api/answers/ask", json={"question": "解释采样定理"}).json()
    client.post("/api/answers/ask", json={"conversation_id": first["conversation_id"], "question": "第二轮为什么需要这样"})
    second = client.post("/api/answers/ask", json={"question": "奈奎斯特采样条件"}).json()
    custom = client.post("/api/conversations", json={"title": "用户自定义专题"}).json()
    client.post("/api/answers/ask", json={"conversation_id": custom["id"], "question": "采样定理"})
    _model_transport(monkeypatch)
    named = client.post("/api/answers/ask", json={"question": "采样定理"}).json()
    empty = client.post("/api/conversations", json={}).json()
    before = client.get("/api/conversations").json()
    with SessionLocal() as session:
        snapshots = {record.id: record.result_json for record in session.scalars(select(AnswerRecord))}
    calls = _model_transport(monkeypatch, title="采样信号的混叠原理")
    ids = [first["conversation_id"], second["conversation_id"], custom["id"], named["conversation_id"], empty["id"], 999999]
    response = client.post("/api/conversations/titles", json={"conversation_ids": ids})
    assert response.status_code == 200
    assert len(calls) == 1
    model_inputs = json.loads(calls[0]["input"])["conversations"]
    assert {item["conversation_id"] for item in model_inputs} == set(ids[:2])
    first_input = next(item for item in model_inputs if item["conversation_id"] == first["conversation_id"])
    assert first_input["question"] == first["question"] and first_input["answer"] == first["answer"]
    assert all(item["question"] != "第二轮为什么需要这样" for item in model_inputs)
    titles = {item["id"]: item for item in response.json()}
    assert 999999 not in titles
    assert all(titles[conversation_id]["title_source"] == "model" for conversation_id in ids[:2])
    assert titles[custom["id"]]["title"] == custom["title"]
    assert titles[named["conversation_id"]]["title"] == named["conversation_title"]
    assert titles[empty["id"]]["title_source"] == "pending"
    after = client.get("/api/conversations").json()
    assert [(item["id"], item["updated_at"]) for item in after] == [(item["id"], item["updated_at"]) for item in before]
    replay = client.get(f"/api/answers/{first['answer_id']}").json()
    assert replay["conversation_title"] == "采样信号的混叠原理"
    assert replay["answer"] == first["answer"] and replay["evidence"] == first["evidence"]
    with SessionLocal() as session:
        assert {record.id: record.result_json for record in session.scalars(select(AnswerRecord))} == snapshots
    assert client.post("/api/conversations/titles", json={"conversation_ids": ids}).status_code == 200
    assert len(calls) == 1
    assert client.post("/api/conversations/titles", json={"conversation_ids": []}).status_code == 422
    assert client.post("/api/conversations/titles", json={"conversation_ids": list(range(1, 34))}).status_code == 422


def test_batch_title_failure_is_reported_without_inventing_titles(client, monkeypatch):
    _model_transport(monkeypatch, title=None)
    answer = client.post("/api/answers/ask", json={"question": "解释采样定理"}).json()
    before = client.get(f"/api/conversations/{answer['conversation_id']}").json()
    response = client.post("/api/conversations/titles", json={"conversation_ids": [answer["conversation_id"]]})
    assert response.status_code == 503
    assert client.get(f"/api/conversations/{answer['conversation_id']}").json() == before


def test_delete_removes_only_target_conversation_and_associated_records(client, monkeypatch, tmp_path):
    _model_transport(monkeypatch)
    target = client.post("/api/answers/ask", json={"question": "解释采样定理"}).json()
    follow = client.post("/api/answers/ask", json={"conversation_id": target["conversation_id"], "question": "继续解释"}).json()
    other = client.post("/api/answers/ask", json={"question": "奈奎斯特采样定理"}).json()
    independent = client.post("/api/answers/validate", json={"question": "采样定理", "answer": "采样频率应大于最高信号频率的两倍。"}).json()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(local_tools, "ARTIFACT_DIR", artifacts)
    target_png, unrelated_png = artifacts / f"{'a' * 32}.png", artifacts / f"{'b' * 32}.png"
    textbook_png = tmp_path / "original.png"
    for path in (target_png, unrelated_png, textbook_png):
        Image.new("RGB", (2, 2), "white").save(path)
    protected_bytes = (unrelated_png.read_bytes(), textbook_png.read_bytes())
    with SessionLocal() as session:
        material_ids = list(session.scalars(select(Material.id).order_by(Material.id)))
        document_ids = list(session.scalars(select(SourceDocument.id).order_by(SourceDocument.id)))
        assert session.scalar(select(ValidationResultRecord.id).where(ValidationResultRecord.answer_id == target["answer_id"])) is not None
        assert session.scalar(select(EvidenceLink.id).where(EvidenceLink.answer_id == target["answer_id"])) is not None
        record = session.get(AnswerRecord, target["answer_id"])
        record.result_json = {**record.result_json, "tool_results": [{"artifacts": [{"id": "a" * 32}, {"id": "../original"}]}]}
        session.commit()
    response = client.delete(f"/api/conversations/{target['conversation_id']}")
    assert response.status_code == 204 and response.content == b""
    assert not target_png.exists()
    assert (unrelated_png.read_bytes(), textbook_png.read_bytes()) == protected_bytes
    assert client.get(f"/api/conversations/{target['conversation_id']}").status_code == 404
    assert client.delete(f"/api/conversations/{target['conversation_id']}").status_code == 404
    assert {item["id"] for item in client.get("/api/conversations").json()} == {other["conversation_id"]}
    assert client.get(f"/api/answers/{other['answer_id']}").json() == other
    assert client.get(f"/api/answers/{independent['answer_id']}").json() == independent
    removed = [target["answer_id"], follow["answer_id"]]
    for answer_id in removed:
        assert client.get(f"/api/answers/{answer_id}").status_code == 404
    with SessionLocal() as session:
        assert session.scalar(select(ConversationTurn.id).where(ConversationTurn.conversation_id == target["conversation_id"])) is None
        assert session.scalar(select(ValidationResultRecord.id).where(ValidationResultRecord.answer_id.in_(removed))) is None
        assert session.scalar(select(EvidenceLink.id).where(EvidenceLink.answer_id.in_(removed))) is None
        assert list(session.scalars(select(Material.id).order_by(Material.id))) == material_ids
        assert list(session.scalars(select(SourceDocument.id).order_by(SourceDocument.id))) == document_ids


@pytest.mark.parametrize("operation", ["ask", "titles"])
def test_delete_while_model_waits_does_not_resurrect_or_attach_to_reused_id(client, monkeypatch, operation):
    _model_transport(monkeypatch, title=None)
    target = client.post("/api/answers/ask", json={"question": "解释采样定理"}).json()
    conversation_id = target["conversation_id"]
    entered, release = Event(), Event()
    _model_transport(monkeypatch, entered=entered, release=release)
    endpoint = "/api/answers/ask" if operation == "ask" else "/api/conversations/titles"
    body = {"conversation_id": conversation_id, "question": "继续解释"} if operation == "ask" else {"conversation_ids": [conversation_id]}
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.post, endpoint, json=body)
        try:
            assert entered.wait(10)
            assert client.delete(f"/api/conversations/{conversation_id}").status_code == 204
            replacement = client.post("/api/conversations", json={}).json()
        finally:
            release.set()
        response = request.result(timeout=15)
    if operation == "ask":
        assert response.status_code == 404
    else:
        assert response.status_code == 200 and response.json() == []
    detail = client.get(f"/api/conversations/{replacement['id']}").json()
    assert detail["turns"] == [] and detail["title_source"] == "pending"
    assert client.get(f"/api/answers/{target['answer_id']}").status_code == 404
    assert client.get("/api/conversations").json() == []
    with SessionLocal() as session:
        assert list(session.scalars(select(AnswerRecord.id))) == []
