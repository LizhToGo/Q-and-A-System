from __future__ import annotations

import re
from typing import Any

from .types import Evidence, ValidationResult


FEATURE_PATTERNS = {
    "frequency_hz": re.compile(r"(?:频率|峰值|主频)[^\d-]*(-?\d+(?:\.\d+)?)\s*(kHz|MHz|Hz)?", re.I),
    "amplitude": re.compile(r"(?:幅值|振幅|幅度)[^\d-]*(-?\d+(?:\.\d+)?)\s*(V|mV)?", re.I),
    "period_s": re.compile(r"(?:周期)[^\d-]*(-?\d+(?:\.\d+)?)\s*(ms|us|s)?", re.I),
}


def _to_base(value: float, unit: str | None, feature: str) -> float:
    unit = (unit or "").lower()
    if feature == "frequency_hz":
        return value * {"khz": 1000, "mhz": 1_000_000}.get(unit, 1)
    if feature == "amplitude":
        return value * {"mv": 0.001}.get(unit, 1)
    if feature == "period_s":
        return value * {"ms": 0.001, "us": 0.000001}.get(unit, 1)
    return value


def _answer_features(answer: str) -> dict[str, float]:
    features: dict[str, float] = {}
    for name, pattern in FEATURE_PATTERNS.items():
        match = pattern.search(answer)
        if match:
            features[name] = _to_base(float(match.group(1)), match.group(2), name)
    return features


def validate_cross_modal(question: str, answer: str, evidence: list[Evidence]) -> ValidationResult:
    modality = [item for item in evidence if (item.metadata or {}).get("modality")]
    if not modality:
        return ValidationResult("cross_modal", "not_applicable", "当前证据没有可比较的波形或频谱结构化特征")
    answer_features = _answer_features(answer)
    comparisons: list[dict[str, Any]] = []
    conflicts: list[str] = []
    evidence_ids: list[int] = []
    for item in modality:
        expected = (item.metadata or {}).get("modality", {})
        for feature, target in expected.items():
            if feature not in answer_features:
                continue
            try:
                target_value = float(target)
            except (TypeError, ValueError):
                continue
            actual = answer_features[feature]
            comparisons.append({"feature": feature, "expected": target_value, "actual": actual, "material_id": item.material_id})
            evidence_ids.append(item.material_id)
            if abs(actual - target_value) > max(abs(target_value) * 0.02, 1e-9):
                conflicts.append(f"{feature} 与资料不一致：资料 {target_value}，回答 {actual}")
    if conflicts:
        return ValidationResult("cross_modal", "conflict", "；".join(conflicts), claim=answer, evidence_ids=list(dict.fromkeys(evidence_ids)), details={"comparisons": comparisons})
    if not comparisons:
        return ValidationResult("cross_modal", "not_applicable", "回答没有提供与波形/频谱特征对应的可比较属性", evidence_ids=[e.material_id for e in modality[:3]])
    return ValidationResult("cross_modal", "pass", "回答中的波形/频谱属性与结构化资料一致", claim=answer, evidence_ids=list(dict.fromkeys(evidence_ids)), details={"comparisons": comparisons})
