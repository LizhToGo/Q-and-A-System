from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

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



class SourceDocumentOut(BaseModel):
    id: int
    course_id: int | None
    filename: str
    parser: str
    status: str
    page_count: int
    job_id: int | None = None
    rag_indexed_pages: int = 0
    rag_verified_pages: int = 0
    rag_candidate_pages: int = 0
    rag_coverage: float = 0.0

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
    source_document_id: int | None = None
    material_types: list[str] | None = None
    limit: int = Field(default=8, ge=1, le=50)

class EvidenceMedia(BaseModel):
    id: str
    kind: Literal["figure", "table", "page"]
    label: str
    page_number: int
    url: str


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
    source_document_id: int | None = None
    content_format: Literal["markdown", "text"] = "text"
    full_content: str | None = None
    page_image_url: str | None = None
    media: list[EvidenceMedia] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    results: list[EvidenceItem]
    has_evidence: bool

ValidationStatus = Literal["pass", "fail", "unknown", "invalid", "conflict", "not_applicable"]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = Field(default=None, gt=0)
    course_id: int | None = None
    topic_id: int | None = None
    source_document_id: int | None = None
    use_web: bool = False
    provider_id: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=200)


class ValidateRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=12000)
    course_id: int | None = None
    topic_id: int | None = None
    material_ids: list[int] | None = None
    source_document_id: int | None = None


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


class ToolArtifactOut(BaseModel):
    id: str
    url: str
    caption: str


class LocalToolResultOut(BaseModel):
    id: str
    tool: str
    status: Literal["completed", "error"]
    title: str
    summary: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ToolArtifactOut] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class StudySuggestionsRequest(BaseModel):
    course_id: int | None = None
    topic_id: int | None = None
    source_document_id: int | None = None
    provider_id: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    exclude: list[Annotated[str, Field(max_length=240)]] = Field(default_factory=list, max_length=12)


class StudySuggestionsOut(BaseModel):
    suggestions: list[str]
    provider: str
    model: str
    evidence_ids: list[int]


class ValidateResponse(BaseModel):
    answer_id: int
    conversation_id: int | None = None
    question: str
    answer: str
    checks: list[ValidationResultOut]
    evidence: list[EvidenceItem]
    confidence: ConfidenceOut
    risk_flags: list[str]
    traceability: dict[str, Any]
    agent_trace: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[LocalToolResultOut] = Field(default_factory=list)


class ConversationCreate(BaseModel):
    course_id: int | None = None
    topic_id: int | None = None
    source_document_id: int | None = None


class ConversationOut(ConversationCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    turns: list[ValidateResponse] = Field(default_factory=list)


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
    quality_review_cleared: bool = False


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
