from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
            "status": self.status,
            "message": self.message,
            "claim": self.claim,
            "evidence_ids": self.evidence_ids,
            "details": self.details,
        }
