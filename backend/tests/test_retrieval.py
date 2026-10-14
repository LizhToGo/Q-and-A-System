from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from backend.app.db import Base
from backend.app.models import Course, Formula, Material, MaterialChunk, QaCase, RetrievalRevision, SourceDocument, Topic
from backend.app.services.agent import ValidationAgent
from backend.app.services.retrieval import evidence_dict, search_evidence
from backend.app.services.rag_worker import RetrievalError, worker_for


@pytest.fixture
def retrieval_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            Course(id=1, name="信号与系统"),
            Course(id=2, name="其他课程"),
            Topic(id=1, course_id=1, name="卷积"),
            Topic(id=2, course_id=2, name="其他主题"),
            SourceDocument(id=1, course_id=1, filename="signals.pdf", file_hash="a", path="signals.pdf"),
            SourceDocument(id=2, course_id=2, filename="other.pdf", file_hash="b", path="other.pdf"),
        ])
        db.flush()
        yield db
    engine.dispose()


def _material(db, *, chunks=("alpha",), formulae=(), **values):
    material = Material(**{
        "course_id": 1,
        "topic_id": 1,
        "source_document_id": 1,
        "material_type": "text",
        "title": "",
        "content": "alpha",
        "section": "",
        "source_anchor": "",
        "verified": True,
        **values,
    })
    db.add(material)
    db.flush()
    for index, text in enumerate(chunks):
        db.add(MaterialChunk(material_id=material.id, chunk_index=index, text=text))
    for raw_formula in formulae:
        db.add(Formula(material_id=material.id, raw_formula=raw_formula, canonical_expression=raw_formula))
    db.flush()
    return material


def test_term_cap_verified_ties_chunk_order_and_full_math_payload(retrieval_db):
    db = retrieval_db
    unverified = _material(db, content="alpha " * 20, verified=False)
    formula = r"\[x=\frac{1}{2}" + "+0" * 50 + r"\]"
    metadata = {"reviewed_formulas": [{"source": "x=1/2", "conditions": "已审核"}], "content_format": "markdown"}
    verified = _material(
        db, title="ALPHA", content="alpha", tags=["alpha"],
        chunks=("说明。" * 395 + formula, "alpha " * 30),
        formulae=("x=1/2", "y=2"), metadata_json=metadata,
    )
    _material(db, content="alpha " * 100, chunks=())

    evidence = search_evidence(db, "Alpha")
    assert [item.material_id for item in evidence] == [verified.id, unverified.id]
    assert [item.score for item in evidence] == [6.0, 6.0]
    assert evidence[0].text.endswith(formula)
    assert evidence[0].formulae == ["x=1/2", "y=2"]
    assert evidence[0].metadata == metadata
    assert evidence[0].course == "信号与系统"
    assert evidence[0].topic == "卷积"
    assert formula in evidence_dict(evidence[0])["snippet"]


def test_chunk_counts_complete_material_counts_before_capping(retrieval_db):
    material = _material(retrieval_db, title="alpha", content="alpha", chunks=("无匹配", "alpha " * 20))
    evidence = search_evidence(retrieval_db, "alpha")
    assert [item.material_id for item in evidence] == [material.id]
    assert evidence[0].score == 6.0
    assert evidence[0].text == "alpha " * 20


@pytest.mark.parametrize("scope", [
    {"course_id": 1}, {"topic_id": 1}, {"source_document_id": 1},
    {"material_types": ["text"]}, {"material_ids": [1]},
    {"course_id": 1, "topic_id": 1, "source_document_id": 1, "material_types": ["text"], "material_ids": [1]},
])
def test_retrieval_keeps_requested_scope(retrieval_db, scope):
    target = _material(retrieval_db)
    _material(
        retrieval_db, course_id=2, topic_id=2, source_document_id=2,
        material_type="formula", content="alpha " * 20,
    )
    assert [item.material_id for item in search_evidence(retrieval_db, "alpha", **scope)] == [target.id]


def test_revoked_source_does_not_hide_active_duplicate_or_shrink_coverage(retrieval_db):
    db = retrieval_db
    _material(db, source_anchor="shared", metadata_json={"publication_status": "revoked"})
    active = _material(db, source_anchor="shared")
    _material(db, source_anchor="shared")
    other = _material(db, source_anchor="other")
    evidence = search_evidence(db, "alpha", limit=2)
    assert [item.material_id for item in evidence] == [active.id, other.id]
    assert [item.source_anchor for item in evidence] == ["shared", "other"]


def test_validation_reuses_actual_evidence_and_checks_requested_scope(retrieval_db):
    db = retrieval_db
    for _ in range(12):
        _material(db, formulae=("x=2",))
    supplied = search_evidence(db, "alpha", limit=20)
    reused, results, _, _, _ = ValidationAgent(db).run("alpha", "x=2", retrieved_evidence=supplied)
    assert len(reused) == 10
    assert all(actual is expected for actual, expected in zip(reused, supplied))
    assert next(result for result in results if result.category == "formula").status == "pass"
    requested = [supplied[-1].material_id]
    filtered, _, _, _, _ = ValidationAgent(db).run("alpha", "x=2", material_ids=requested, retrieved_evidence=supplied)
    assert filtered == [supplied[-1]]
    for evidence, material_ids in (([], None), (supplied, []), (supplied, [-1])):
        empty, results, _, _, _ = ValidationAgent(db).run("alpha", "x=2", material_ids=material_ids, retrieved_evidence=evidence)
        assert empty == []
        assert next(result for result in results if result.category == "formula").status == "unknown"


def test_standalone_validation_still_retrieves_and_checks_requested_material(retrieval_db):
    db = retrieval_db
    correct = _material(db, formulae=("x=2",))
    wrong = _material(db, formulae=("x=3",))
    for reference, expected in ((correct, "pass"), (wrong, "fail")):
        evidence, results, _, _, _ = ValidationAgent(db).run("alpha", "x=2", material_ids=[reference.id])
        assert [item.material_id for item in evidence] == [reference.id]
        assert next(result for result in results if result.category == "formula").status == expected


def test_chinese_substrings_empty_filters_and_python_slice_limits(retrieval_db):
    db = retrieval_db
    first = _material(db, content="采样定理要求频率足够高", chunks=("教材正文",))
    second = _material(db, content="采样率", chunks=("采样率",))
    third = _material(db, content="采样条件", chunks=("采样条件",))
    assert [item.material_id for item in search_evidence(db, "采样定理")] == [first.id, second.id, third.id]
    assert [item.material_id for item in search_evidence(db, "采样", material_types=[], material_ids=[])] == [second.id, third.id, first.id]
    assert [item.material_id for item in search_evidence(db, "采样", limit=-1)] == [second.id, third.id]
    assert search_evidence(db, "采样", limit=0) == []
    assert search_evidence(db, "采样", limit=-100) == []
    assert search_evidence(db, " \n x 1 ! ") == []


def test_raw_sql_refreshes_publication_chunks_and_deletion(retrieval_db):
    db = retrieval_db
    material = _material(db)
    material_id = material.id
    db.commit()
    assert search_evidence(db, "alpha")[0].material_id == material_id
    db.execute(text("UPDATE materials SET content = 'beta', verified = 0 WHERE id = :id"), {"id": material_id})
    db.execute(text("UPDATE material_chunks SET text = 'beta' WHERE material_id = :id"), {"id": material_id})
    db.commit()
    assert search_evidence(db, "alpha") == []
    assert search_evidence(db, "beta")[0].verified is False
    db.execute(text("""UPDATE materials SET metadata_json = '{"publication_status":"revoked"}' WHERE id = :id"""), {"id": material_id})
    db.commit()
    assert search_evidence(db, "beta") == []
    db.execute(text("""UPDATE materials SET metadata_json = '{"publication_status":"published"}', verified = 1 WHERE id = :id"""), {"id": material_id})
    db.commit()
    assert search_evidence(db, "beta")[0].verified is True
    db.execute(text("DELETE FROM material_chunks WHERE material_id = :id"), {"id": material_id})
    db.commit()
    assert search_evidence(db, "beta") == []
    db.execute(text("INSERT INTO material_chunks (material_id, chunk_index, text, keywords) VALUES (:id, 0, 'gamma', '[]')"), {"id": material_id})
    db.commit()
    assert search_evidence(db, "gamma")[0].text == "gamma"
    db.execute(text("DELETE FROM materials WHERE id = :id"), {"id": material_id})
    db.commit()
    assert search_evidence(db, "gamma") == []


def test_rollback_and_counter_reuse_never_reuses_uncommitted_corpus(retrieval_db):
    db = retrieval_db
    material_id = _material(db).id
    db.commit()
    assert search_evidence(db, "alpha")
    for term in ("beta", "gamma"):
        db.execute(text("UPDATE material_chunks SET text = :term WHERE material_id = :id"), {"term": term, "id": material_id})
        assert search_evidence(db, term)[0].text == term
        db.rollback()
    assert search_evidence(db, "beta") == []
    assert search_evidence(db, "gamma") == []
    assert search_evidence(db, "alpha")[0].text == "alpha"


def test_search_does_not_autoflush_or_commit_caller_changes(retrieval_db):
    db = retrieval_db
    material = _material(db)
    material_id = material.id
    db.commit()
    db.autoflush = True
    material.title = "pending title"
    pending = QaCase(case_id="pending", course_id=1, topic_id=1, question="question", answer="answer",
                     expected_status="pass", expected_category="formula")
    db.add(pending)
    db.connection().execute(text("UPDATE material_chunks SET text = 'beta' WHERE material_id = :id"), {"id": material_id})
    evidence = search_evidence(db, "beta")[0]
    assert evidence.text == "beta"
    assert evidence.title == ""
    assert pending.id is None
    assert material in db.dirty
    assert db.connection().connection.driver_connection.in_transaction
    db.rollback()
    assert search_evidence(db, "beta") == []
    assert search_evidence(db, "alpha")[0].title == ""


def test_qa_writes_preserve_retrieval_and_hydrate_current_names_and_formulas(retrieval_db):
    db = retrieval_db
    material_id = _material(db).id
    db.commit()
    assert search_evidence(db, "alpha")
    db.add(QaCase(case_id="case", course_id=1, topic_id=1, question="q", answer="a",
                  expected_status="pass", expected_category="formula"))
    db.add(Formula(material_id=material_id, raw_formula="x=2", canonical_expression="x=2"))
    db.execute(text("UPDATE courses SET name = '当前课程' WHERE id = 1"))
    db.execute(text("UPDATE knowledge_topics SET name = '当前主题' WHERE id = 1"))
    db.commit()
    evidence = search_evidence(db, "alpha")[0]
    assert (evidence.course, evidence.topic, evidence.formulae) == ("当前课程", "当前主题", ["x=2"])


def test_metadata_creation_is_idempotent_and_reset_changes_generation(retrieval_db):
    db = retrieval_db
    _material(db)
    db.commit()
    assert search_evidence(db, "alpha")
    before = db.execute(select(RetrievalRevision.generation, RetrievalRevision.revision)).one()
    db.rollback()
    Base.metadata.create_all(db.get_bind())
    after = db.execute(select(RetrievalRevision.generation, RetrievalRevision.revision)).one()
    assert before == after
    assert search_evidence(db, "alpha")
    db.rollback()
    Base.metadata.drop_all(db.get_bind())
    Base.metadata.create_all(db.get_bind())
    db.add(Course(id=1, name="重建课程"))
    db.flush()
    _material(db, topic_id=None, source_document_id=None, content="beta", chunks=("beta",))
    db.commit()
    reset = db.execute(select(RetrievalRevision.generation, RetrievalRevision.revision)).one()
    assert reset.generation != before.generation
    assert reset.revision == before.revision
    assert search_evidence(db, "alpha") == []
    assert search_evidence(db, "beta")[0].course == "重建课程"


def test_concurrent_commit_cannot_mix_ranked_and_hydrated_snapshots(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}")
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    try:
        with Session(engine) as db:
            db.add(Course(id=1, name="课程"))
            db.flush()
            material_id = _material(db, topic_id=None, source_document_id=None).id
            db.commit()
            worker = worker_for(engine)
            original = worker.request

            def commit_after_query(payload):
                matches = original(payload)
                with engine.begin() as writer:
                    writer.execute(text("UPDATE material_chunks SET text = 'beta' WHERE material_id = :id"), {"id": material_id})
                return matches

            monkeypatch.setattr(worker, "request", commit_after_query)
            assert search_evidence(db, "alpha")[0].text == "alpha"
            monkeypatch.setattr(worker, "request", original)
            assert search_evidence(db, "beta")[0].text == "beta"
    finally:
        engine.dispose()


def test_worker_exit_is_visible_and_next_request_starts_a_new_worker(retrieval_db):
    db = retrieval_db
    _material(db)
    db.commit()
    assert search_evidence(db, "alpha")
    worker = worker_for(db.get_bind())
    worker._process.kill()
    worker._process.wait(timeout=5)
    with pytest.raises(RetrievalError, match="exited unexpectedly"):
        search_evidence(db, "alpha")
    assert search_evidence(db, "alpha")


@pytest.mark.parametrize("endpoint,payload", [
    ("/api/search", {"query": "采样定理"}),
    ("/api/answers/validate", {"question": "采样定理", "answer": "采样频率"}),
    ("/api/answers/ask", {"question": "采样定理"}),
])
def test_api_worker_failure_is_not_empty_evidence_or_model_fallback(client, monkeypatch, tmp_path, endpoint, payload):
    from dataclasses import replace
    from backend.app.services import rag_worker

    rag_worker.close_workers()
    monkeypatch.setattr(rag_worker, "settings", replace(rag_worker.settings, rag_engine_path=str(tmp_path / "missing.dll")))
    response = client.post(endpoint, json=payload)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "retrieval_unavailable"


def test_legacy_answer_rehydrates_through_real_worker_and_reports_worker_failure(client, monkeypatch, tmp_path):
    from dataclasses import replace
    from backend.app.db import SessionLocal
    from backend.app.models import AnswerRecord, ValidationResultRecord
    from backend.app.services import rag_worker

    with SessionLocal() as db:
        material = _material(db, content="legacyalpha", chunks=("legacyalpha",))
        material_id = material.id
        record = AnswerRecord(question="legacyalpha", answer="历史回答", course_id=1, topic_id=1, result_json={})
        db.add(record)
        db.flush()
        answer_id = record.id
        db.add(ValidationResultRecord(answer_id=answer_id, category="formula", status="unknown", message="历史校验",
                                      evidence_ids=[material_id]))
        db.commit()
    response = client.get(f"/api/answers/{answer_id}")
    assert response.status_code == 200
    assert [item["material_id"] for item in response.json()["evidence"]] == [material_id]
    assert response.json()["evidence"][0]["snippet"] == "legacyalpha"
    rag_worker.close_workers()
    monkeypatch.setattr(rag_worker, "settings", replace(rag_worker.settings, rag_engine_path=str(tmp_path / "missing.dll")))
    failed = client.get(f"/api/answers/{answer_id}")
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "retrieval_unavailable"


def test_worker_environment_never_forwards_application_credentials(monkeypatch):
    from backend.app.services.rag_worker import worker_environment

    for name in ("LLM_API_KEY", "WEB_SEARCH_API_KEY", "OPENAI_API_KEY", "PROVIDER_TOKEN", "DATABASE_URL"):
        monkeypatch.setenv(name, "private-value")
    environment = worker_environment()
    assert "private-value" not in environment.values()
    assert not any("KEY" in name.upper() or "TOKEN" in name.upper() for name in environment)


def test_existing_database_migration_preserves_rows_and_installs_raw_write_tracking(retrieval_db):
    db = retrieval_db
    material_id = _material(db, metadata_json={"reviewed_formulas": [{"source": "x=2"}]},
                            formulae=("x=2",)).id
    db.commit()
    engine = db.get_bind()
    with engine.begin() as connection:
        before = connection.execute(select(Material.__table__)).all()
        for table in ("materials", "material_chunks"):
            for operation in ("insert", "update", "delete"):
                connection.exec_driver_sql(f"DROP TRIGGER retrieval_{table}_{operation}")
        connection.exec_driver_sql("DROP TABLE retrieval_revision")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        assert connection.execute(select(Material.__table__)).all() == before
        assert connection.execute(select(Formula.raw_formula)).scalars().all() == ["x=2"]
        assert connection.execute(select(RetrievalRevision.revision)).scalar_one() == 0
        connection.execute(text("UPDATE material_chunks SET text = 'migrated' WHERE material_id = :id"), {"id": material_id})
        assert connection.execute(select(RetrievalRevision.revision)).scalar_one() == 1
    evidence = search_evidence(db, "migrated")[0]
    assert evidence.material_id == material_id
    assert evidence.metadata == {"reviewed_formulas": [{"source": "x=2"}]}
    assert evidence.formulae == ["x=2"]


def test_engine_disposal_closes_owned_worker(retrieval_db):
    db = retrieval_db
    _material(db)
    db.commit()
    assert search_evidence(db, "alpha")
    worker = worker_for(db.get_bind())
    process = worker._process
    db.rollback()
    db.get_bind().dispose()
    assert process.poll() is not None
    assert worker.revision is None
