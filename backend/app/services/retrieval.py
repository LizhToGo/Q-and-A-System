from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Course, Formula, Material, MaterialChunk, Topic
from .validators.types import Evidence
from .math_markup import safe_excerpt


TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]{1,}")


def normalize_query(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def query_terms(value: str) -> list[str]:
    normalized = normalize_query(value)
    terms: list[str] = []
    for token in TOKEN_RE.findall(normalized):
        if token.isascii():
            if len(token) > 1:
                terms.append(token)
            continue
        terms.append(token)
        if all("\u4e00" <= char <= "\u9fff" for char in token):
            for width in (2, 3):
                terms.extend(token[index:index + width] for index in range(max(0, len(token) - width + 1)))
    return list(dict.fromkeys(term for term in terms if len(term) > 1))


def search_evidence(
    db: Session,
    query: str,
    course_id: int | None = None,
    topic_id: int | None = None,
    material_types: list[str] | None = None,
    material_ids: list[int] | None = None,
    source_document_id: int | None = None,
    limit: int = 8,
) -> list[Evidence]:
    terms = query_terms(query)
    if not terms:
        return []

    # Rank with scalar columns only: review metadata can contain entire page
    # payloads and must not be decoded for every candidate (or every chunk).
    statement = select(
        Material.id,
        Material.topic_id,
        Material.title,
        Material.content,
        Material.tags,
        Material.section,
        Material.verified,
        Material.source_anchor,
    ).where(Material.id.in_(select(MaterialChunk.material_id)))
    if course_id is not None:
        statement = statement.where(Material.course_id == course_id)
    if topic_id is not None:
        statement = statement.where(Material.topic_id == topic_id)
    if material_types:
        statement = statement.where(Material.material_type.in_(material_types))
    if material_ids:
        statement = statement.where(Material.id.in_(material_ids))
    if source_document_id is not None:
        statement = statement.where(Material.source_document_id == source_document_id)

    materials = {material.id: material for material in db.execute(statement)}
    if not materials:
        return []

    # Query terms contain no spaces, so no match can cross a joined field
    # boundary. Count material-level fields once, then apply the original
    # per-term cap to their combined counts with each chunk.
    material_scores: dict[int, tuple[float, list[tuple[str, int]]]] = {}
    for material in materials.values():
        shared_text = " ".join(
            [material.title, material.content, " ".join(material.tags or []), material.section]
        ).lower()
        counts = [(term, min(3, shared_text.count(term))) for term in terms]
        material_scores[material.id] = (
            float(2 * sum(count for _, count in counts)),
            [(term, count) for term, count in counts if count < 3],
        )

    chunks = select(
        MaterialChunk.id, MaterialChunk.material_id, MaterialChunk.chunk_index, MaterialChunk.text
    ).where(MaterialChunk.material_id.in_(statement.with_only_columns(Material.id)))
    ranked = []
    for chunk in db.execute(chunks):
        material = materials.get(chunk.material_id)
        if material is None:
            continue
        base_score, remaining = material_scores[material.id]
        text = chunk.text.lower()
        score = base_score + 2 * sum(min(3 - count, text.count(term)) for term, count in remaining)
        if score > 0:
            ranked.append((score, chunk, material))
    ranked.sort(key=lambda item: (-item[0], int(not item[2].verified), item[2].id, item[1].chunk_index))
    best_by_material = {}
    for score, chunk, material in ranked:
        best_by_material.setdefault(material.id, (score, chunk, material))
    candidates = list(best_by_material.values())
    best_by_source = {}
    # Hydrate only leading candidates. Revoked sources are skipped before
    # deduplication, and further batches fill any resulting gaps in coverage.
    batch_size = max(1, limit) if limit >= 0 else max(1, len(candidates))
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset:offset + batch_size]
        details = {
            row.id: row
            for row in db.execute(
                select(
                    Material.id,
                    Material.material_type,
                    Material.page_number,
                    Material.metadata_json,
                    Course.name.label("course_name"),
                    Topic.name.label("topic_name"),
                )
                .outerjoin(Course, Course.id == Material.course_id)
                .outerjoin(Topic, Topic.id == Material.topic_id)
                .where(Material.id.in_([material.id for _, _, material in batch]))
            )
        }
        for score, chunk, material in batch:
            detail = details[material.id]
            if (detail.metadata_json or {}).get("publication_status") == "revoked":
                continue
            source_key = material.source_anchor or f"material:{material.id}"
            best_by_source.setdefault(source_key, (score, chunk, material, detail))
        if limit >= 0 and len(best_by_source) >= limit:
            break
    selected = list(best_by_source.values())[:limit]
    if not selected:
        return []

    selected_ids = [material.id for _, _, material, _ in selected]
    formulae: dict[int, list[str]] = {material_id: [] for material_id in selected_ids}
    for material_id, raw_formula in db.execute(
        select(Formula.material_id, Formula.raw_formula)
        .where(Formula.material_id.in_(selected_ids))
        .order_by(Formula.id)
    ):
        formulae[material_id].append(raw_formula)
    results: list[Evidence] = []
    for score, chunk, material, detail in selected:
        results.append(
            Evidence(
                material_id=material.id,
                chunk_id=chunk.id,
                course=detail.course_name if detail.course_name is not None else "未知课程",
                topic=detail.topic_name if material.topic_id else None,
                title=material.title,
                material_type=detail.material_type,
                page_number=detail.page_number,
                section=material.section,
                source_anchor=material.source_anchor,
                text=chunk.text,
                score=round(score, 4),
                verified=material.verified,
                metadata=detail.metadata_json or {},
                formulae=formulae[material.id],
            )
        )
    return results


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
