from __future__ import annotations

from sqlalchemy.orm import Session

from ..config import settings
from .confidence import calculate_confidence
from .retrieval import search_evidence
from .traceability import build_traceability
from .validators.cross_modal_validator import validate_cross_modal
from .validators.formula_validator import validate_formula
from .validators.numeric_validator import validate_numeric
from .validators.text_validator import validate_text
from .validators.types import ValidationResult


class ValidationAgent:
    """有限状态的 RAG 编排器；模型只能作为可选辅助，不直接写库或裁决。"""

    def __init__(self, db: Session):
        self.db = db
        self.trace: list[dict] = []

    def _step(self, name: str, status: str, **details) -> None:
        self.trace.append({"step": name, "status": status, **details})

    def run(self, question: str, answer: str, course_id=None, topic_id=None, material_ids=None):
        self._step("classify", "completed", categories=["text", "formula", "numeric", "cross_modal"])
        evidence = search_evidence(self.db, f"{question} {answer}", course_id, topic_id, material_ids=material_ids, limit=10)
        self._step("retrieve_local", "completed", evidence_ids=[item.material_id for item in evidence], count=len(evidence))
        if not evidence:
            self._step("fallback", "completed", reason="local evidence unavailable")
        results: list[ValidationResult] = [
            validate_text(question, answer, evidence),
            validate_formula(question, answer, evidence),
            validate_numeric(question, answer, evidence),
            validate_cross_modal(question, answer, evidence),
        ]
        self._step("deterministic_validation", "completed", statuses={result.category: result.status for result in results})
        confidence = calculate_confidence(results, len(evidence))
        traceability = build_traceability(evidence, results)
        self._step("finalize", "completed", confidence=confidence["level"], traceability=traceability["status"])
        return evidence, results, confidence, traceability, self.trace
