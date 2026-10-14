from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from ..models import Course, Formula, Material, MaterialChunk, RetrievalRevision, Topic
from .math_markup import safe_excerpt
from .rag_worker import RetrievalError, worker_for
from .validators.types import Evidence


def _snapshot(connection: Connection, revision: str) -> dict:
    # Only ranking fields cross the process boundary. Page/review payloads and
    # formula metadata remain in Python and are hydrated for selected rows only.
    materials = {}
    for row in connection.execute(select(
        Material.id, Material.course_id, Material.topic_id, Material.source_document_id,
        Material.material_type, Material.title, Material.content, Material.tags,
        Material.section, Material.source_anchor, Material.verified,
        func.json_extract(Material.metadata_json, "$.publication_status").label("publication_status"),
    ).where(Material.id.in_(select(MaterialChunk.material_id))).order_by(Material.id)):
        materials[row.id] = {
            "id": row.id, "course_id": row.course_id, "topic_id": row.topic_id,
            "source_document_id": row.source_document_id, "material_type": row.material_type,
            "title": row.title, "content": row.content, "tags": row.tags or [],
            "section": row.section, "source_anchor": row.source_anchor,
            "verified": row.verified, "revoked": row.publication_status == "revoked", "chunks": [],
        }
    for row in connection.execute(select(
        MaterialChunk.id, MaterialChunk.material_id, MaterialChunk.chunk_index, MaterialChunk.text,
    ).order_by(MaterialChunk.id)):
        material = materials.get(row.material_id)
        if material is not None:
            material["chunks"].append({"id": row.id, "chunk_index": row.chunk_index, "text": row.text})
    return {"revision": revision, "materials": list(materials.values())}


def _hydrate(connection: Connection, matches: list[dict]) -> list[Evidence]:
    if not matches:
        return []
    material_ids = [match["material_id"] for match in matches]
    chunk_ids = [match["chunk_id"] for match in matches]
    details = {
        row.id: row for row in connection.execute(select(
            Material.id, Material.topic_id, Material.title, Material.material_type,
            Material.page_number, Material.section, Material.source_anchor,
            Material.verified, Material.metadata_json,
            Course.name.label("course_name"), Topic.name.label("topic_name"),
        ).outerjoin(Course, Course.id == Material.course_id)
         .outerjoin(Topic, Topic.id == Material.topic_id)
         .where(Material.id.in_(material_ids)))
    }
    chunks = {row.id: row for row in connection.execute(select(
        MaterialChunk.id, MaterialChunk.material_id, MaterialChunk.text,
    ).where(MaterialChunk.id.in_(chunk_ids)))}
    formulae = {material_id: [] for material_id in material_ids}
    for material_id, raw_formula in connection.execute(
        select(Formula.material_id, Formula.raw_formula)
        .where(Formula.material_id.in_(material_ids)).order_by(Formula.id)
    ):
        formulae[material_id].append(raw_formula)
    results = []
    for match in matches:
        material = details.get(match["material_id"])
        chunk = chunks.get(match["chunk_id"])
        if material is None or chunk is None or chunk.material_id != material.id:
            raise RetrievalError("C# retrieval evidence does not belong to the SQLite snapshot")
        results.append(Evidence(
            material_id=material.id, chunk_id=chunk.id,
            course=material.course_name if material.course_name is not None else "未知课程",
            topic=material.topic_name if material.topic_id else None,
            title=material.title, material_type=material.material_type,
            page_number=material.page_number, section=material.section,
            source_anchor=material.source_anchor, text=chunk.text,
            score=round(match["score"], 4), verified=material.verified,
            metadata=material.metadata_json or {}, formulae=formulae[material.id],
        ))
    return results


def search_evidence(
    db: Session,
    query: str,
    course_id: int | None = None,
    topic_id: int | None = None,
    material_types: list[str] | None = None,
    material_ids: list[int] | None = None,
    source_document_id: int | None = None,
    limit: int = 8,
    *,
    additional_queries: list[str] | None = None,
) -> list[Evidence]:
    # Connection-level savepoints do not trigger Session.begin_nested's flush.
    # Explicit BEGIN also makes SQLite's legacy transaction mode hold one read
    # snapshot and prevents RELEASE SAVEPOINT committing the caller's writes.
    with db.no_autoflush:
        connection = db.connection()
        if connection.dialect.name != "sqlite":
            raise RetrievalError("C# retrieval requires a SQLite snapshot")
        worker = worker_for(connection.engine)
        with worker.lock:
            driver = connection.connection.driver_connection
            preexisting_transaction = driver.in_transaction
            if not preexisting_transaction:
                connection.exec_driver_sql("BEGIN")
            savepoint = connection.begin_nested()
            try:
                state = connection.execute(select(RetrievalRevision.generation, RetrievalRevision.revision)
                                           .where(RetrievalRevision.id == 1)).one()
                revision = f"{state.generation}:{state.revision}"
                snapshot = _snapshot(connection, revision) if preexisting_transaction or worker.revision != revision else None
                matches = worker.request({
                    "snapshot": snapshot, "query": query, "course_id": course_id,
                    "topic_id": topic_id, "material_types": material_types,
                    "material_ids": material_ids, "source_document_id": source_document_id,
                    "limit": limit,
                    "additional_queries": additional_queries,
                })
                results = _hydrate(connection, matches)
                savepoint.commit()
                return results
            except Exception:
                savepoint.rollback()
                raise
            finally:
                if preexisting_transaction:
                    # An uncommitted writer may roll back then reuse the same
                    # counter with different data. Never reuse its worker corpus.
                    worker.revision = None
                else:
                    connection.exec_driver_sql("ROLLBACK")


def evidence_dict(evidence: Evidence) -> dict:
    return {
        "material_id": evidence.material_id,
        "chunk_id": evidence.chunk_id,
        "course": evidence.course,
        "topic": evidence.topic,
        "title": evidence.title,
        "material_type": evidence.material_type,
        "page_number": evidence.page_number,
        "section": evidence.section,
        "source_anchor": evidence.source_anchor,
        "snippet": safe_excerpt(evidence.text, 1200),
        "score": evidence.score,
        "verified": evidence.verified,
        "source_document_id": evidence.metadata.get("source_document_id"),
        "content_format": "markdown" if evidence.metadata.get("content_format") == "markdown" else "text",
    }
