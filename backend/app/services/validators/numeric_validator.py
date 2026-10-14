from __future__ import annotations

import re
from typing import Any

from pint import UnitRegistry

from .types import Evidence, ValidationResult

UREG = UnitRegistry(autoconvert_offset_to_baseunit=True)
UNIT_ALIASES = {
    "千赫兹": "kilohertz",
    "khz": "kilohertz",
    "mhz": "megahertz",
    "hz": "hertz",
    "伏": "volt",
    "v": "volt",
    "毫伏": "millivolt",
    "a": "ampere",
    "安": "ampere",
    "欧": "ohm",
    "ohm": "ohm",
    "秒": "second",
    "s": "second",
}
NUMBER_UNIT_RE = re.compile(r"(?<![A-Za-z])(-?\d+(?:\.\d+)?)\s*(千赫兹|MHz|kHz|Hz|毫伏|mV|V|伏|A|安|欧|ohm|s|秒)\b", re.I)


def _quantity(value: float, unit: str):
    return value * UREG(UNIT_ALIASES.get(unit.lower(), UNIT_ALIASES.get(unit, unit)))


def _answer_quantities(answer: str) -> list[tuple[float, str]]:
    return [(float(value), unit) for value, unit in NUMBER_UNIT_RE.findall(answer)]


def _expected(evidence: list[Evidence]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for item in evidence:
        candidates = (item.metadata or {}).get("expected_values", [])
        if isinstance(candidates, dict):
            candidates = [candidates]
        values.extend(candidates)
    return values


def validate_numeric(question: str, answer: str, evidence: list[Evidence]) -> ValidationResult:
    expected = _expected(evidence)
    answer_values = _answer_quantities(answer)
    if not expected and not answer_values:
        return ValidationResult("numeric", "not_applicable", "当前问题没有可校验的带单位数值")
    if expected and not answer_values:
        return ValidationResult("numeric", "missing_unit", "回答没有提供带单位的数值结果", claim=answer, evidence_ids=[e.material_id for e in evidence[:3]], details={"reason": "missing_unit"})
    if not expected:
        return ValidationResult("numeric", "unknown", "发现数值但教材证据没有标准数值", claim=str(answer_values))

    errors: list[str] = []
    matches = 0
    for expected_value in expected:
        try:
            target = _quantity(float(expected_value["value"]), expected_value["unit"])
        except Exception:
            continue
        matched = False
        for value, unit in answer_values:
            try:
                candidate = _quantity(value, unit)
                if not candidate.dimensionality == target.dimensionality:
                    errors.append(f"单位维度错误：回答为 {value} {unit}，期望维度为 {expected_value['unit']}")
                    continue
                tolerance = float(expected_value.get("tolerance", 1e-6))
                if abs(candidate.to(target.units).magnitude - target.magnitude) <= max(tolerance, abs(target.magnitude) * 0.01):
                    matched = True
                    matches += 1
                    break
            except Exception:
                continue
        if not matched and not errors:
            errors.append(f"数值不匹配：期望约 {expected_value['value']} {expected_value['unit']}")
    if errors:
        return ValidationResult("numeric", "fail", "；".join(errors), claim=str(answer_values), evidence_ids=[e.material_id for e in evidence[:3]], details={"matches": matches, "expected": expected})
    return ValidationResult("numeric", "pass", "数值经过单位归一化后与教材标准值一致", claim=str(answer_values), evidence_ids=[e.material_id for e in evidence[:3]], details={"matches": matches, "expected": expected})
