from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event

from PIL import Image
import pytest

from backend.app import main
from backend.app.db import SessionLocal
from backend.app.models import Material, SourceDocument
from backend.app.provider_config import ProviderConfig
from backend.app.services import local_tools
from backend.app.services.answering import LLMAnswerer


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
            return {"answer": "执行本地离散卷积。", "evidence_ids": [], "tool_requests": [{"tool": "convolution", "arguments": {"x": [1, 2], "h": [1, -1], "x_start": -1, "h_start": 2}}]}
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
