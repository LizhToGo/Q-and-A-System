from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Course, Material, MaterialChunk, Topic
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


def _score(text: str, terms: Iterable[str]) -> float:
    lowered = text.lower()
    score = 0.0
    for term in terms:
        count = lowered.count(term.lower())
        if count:
            score += min(3.0, count) * (2.0 if len(term) > 1 else 0.5)
    return score


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

    statement = select(MaterialChunk, Material).join(Material, Material.id == MaterialChunk.material_id)
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

    rows = db.execute(statement).all()
    course_names = {row.id: row.name for row in db.scalars(select(Course))}
    topic_names = {row.id: row.name for row in db.scalars(select(Topic))}
    ranked: list[tuple[float, MaterialChunk, Material]] = []
    for chunk, material in rows:
        searchable = " ".join(
            [material.title, material.content, chunk.text, " ".join(material.tags or []), material.section]
        )
        score = _score(searchable, terms)
        if score > 0:
            ranked.append((score, chunk, material))
    ranked.sort(key=lambda item: (-item[0], int(not item[2].verified), item[2].id, item[1].chunk_index))
    best_by_source: dict[str, tuple[float, MaterialChunk, Material]] = {}
    for score, chunk, material in ranked:
        if (material.metadata_json or {}).get("publication_status") == "revoked":
            continue
        source_key = material.source_anchor or f"material:{material.id}"
        best_by_source.setdefault(source_key, (score, chunk, material))
    results: list[Evidence] = []
    for score, chunk, material in list(best_by_source.values())[:limit]:
        metadata = material.metadata_json or {}
        results.append(
            Evidence(
                material_id=material.id,
                chunk_id=chunk.id,
                course=course_names.get(material.course_id, "未知课程"),
                topic=topic_names.get(material.topic_id) if material.topic_id else None,
                title=material.title,
                material_type=material.material_type,
                page_number=material.page_number,
                section=material.section,
                source_anchor=material.source_anchor,
                text=chunk.text,
                score=round(score, 4),
                verified=material.verified,
                metadata=metadata,
                formulae=[formula.raw_formula for formula in material.formulas],
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
