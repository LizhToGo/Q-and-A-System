from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CourseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    topic_count: int = 0
    material_count: int = 0


class TopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    course_id: int
    name: str
    description: str


class MaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    course_id: int
    topic_id: int | None
    material_type: str
    title: str
    content: str
    page_number: int | None
    section: str
    source_anchor: str
    tags: list[Any] = Field(default_factory=list)
    verified: bool


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    course_id: int | None = None
    topic_id: int | None = None
    material_types: list[str] | None = None
    limit: int = Field(default=8, ge=1, le=50)


class EvidenceItem(BaseModel):
    material_id: int
    chunk_id: int | None = None
    course: str
    topic: str | None
    title: str
    material_type: str
    page_number: int | None
    section: str
    source_anchor: str
    snippet: str
    score: float
    verified: bool


class SearchResponse(BaseModel):
    query: str
    results: list[EvidenceItem]
    has_evidence: bool


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    course_id: int | None = None
    topic_id: int | None = None
    use_web: bool = False


class ValidateRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=12000)
    course_id: int | None = None
    topic_id: int | None = None
    material_ids: list[int] | None = None

ValidationStatus = Literal["pass", "fail", "unknown", "invalid", "conflict", "not_applicable"]


class ValidationResultOut(BaseModel):
    category: str
    status: ValidationStatus
    message: str
    claim: str = ""
    evidence_ids: list[int] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class ConfidenceOut(BaseModel):
    level: Literal["high", "medium", "low"]
    score: float
    reasons: list[str]


class ValidateResponse(BaseModel):
    answer_id: int
    question: str
    answer: str
    checks: list[ValidationResultOut]
    evidence: list[EvidenceItem]
    confidence: ConfidenceOut
    risk_flags: list[str]
    traceability: dict[str, Any]
    agent_trace: list[dict[str, Any]] = Field(default_factory=list)


class ExtractedItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    page_number: int
    item_type: str
    raw_text: str
    normalized_text: str
    payload: dict[str, Any]
    confidence: float
    needs_review: bool
    review_status: str
    source_anchor: str


class IngestionJobOut(BaseModel):
    id: int
    source_document_id: int
    status: str
    processed_pages: int
    total_pages: int
    summary: dict[str, Any]
    items: list[ExtractedItemOut] = Field(default_factory=list)


class ReviewItemRequest(BaseModel):
    normalized_text: str | None = None
    payload: dict[str, Any] | None = None
    review_status: Literal["pending", "approved", "rejected"]
    needs_review: bool | None = None


class MetricsOut(BaseModel):
    course_count: int
    topic_count: int
    material_count: int
    verified_material_count: int
    evaluation_case_count: int
    evaluation_run: bool
    traceability_rate: float
    conflict_accuracy: float | None
    false_positive_rate: float | None
    category_stats: dict[str, Any]
