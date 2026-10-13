from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_INTERNAL_ERROR_STATUSES = {"missing_unit", "dimension_error", "mismatch"}


def public_validation_status(status: str) -> str:
    """Map validator-specific error labels to the public API contract."""
    return "fail" if status in _INTERNAL_ERROR_STATUSES else status

@dataclass
class Evidence:
    material_id: int
    chunk_id: int | None
    course: str
    topic: str | None
    title: str
    material_type: str
    page_number: int | None
    section: str
    source_anchor: str
    text: str
    score: float
    verified: bool
    metadata: dict[str, Any] = field(default_factory=dict)
    formulae: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    category: str
    status: str
    message: str
    claim: str = ""
    evidence_ids: list[int] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "status": public_validation_status(self.status),
            "message": self.message,
            "claim": self.claim,
            "evidence_ids": self.evidence_ids,
            "details": self.details,
        }
