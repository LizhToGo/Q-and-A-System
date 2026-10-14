from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend.app.db import Base
from backend.app.models import Course, Formula, Material, MaterialChunk, SourceDocument, Topic
from backend.app.services.agent import ValidationAgent
from backend.app.services.retrieval import evidence_dict, search_evidence


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


def test_validation_reuses_actual_evidence_without_database_queries(retrieval_db):
    db = retrieval_db
    for _ in range(12):
        _material(db, formulae=("x=2",))
    supplied = search_evidence(db, "alpha", limit=20)
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", record_statement)
    try:
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
        assert statements == []
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", record_statement)


def test_standalone_validation_still_retrieves_and_checks_requested_material(retrieval_db):
    db = retrieval_db
    correct = _material(db, formulae=("x=2",))
    wrong = _material(db, formulae=("x=3",))
    for reference, expected in ((correct, "pass"), (wrong, "fail")):
        evidence, results, _, _, _ = ValidationAgent(db).run("alpha", "x=2", material_ids=[reference.id])
        assert [item.material_id for item in evidence] == [reference.id]
        assert next(result for result in results if result.category == "formula").status == expected
