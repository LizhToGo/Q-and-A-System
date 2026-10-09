from __future__ import annotations

import re
from typing import Any

from sympy import Eq, Symbol, simplify
from sympy.parsing.sympy_parser import implicit_multiplication_application, parse_expr, standard_transformations

from .types import Evidence, ValidationResult

TRANSFORMATIONS = standard_transformations + (implicit_multiplication_application,)
FORMULA_RE = re.compile(r"(?:\$([^$]+)\$|\\\((.+?)\\\)|([A-Za-z][A-Za-z0-9_{}]*\s*[<>=]+\s*[^。！？\n,，;；]+))")


def _clean(value: str) -> str:
    value = value.strip().replace("\\,", "").replace("\\cdot", "*")
    value = value.replace("\\times", "*").replace("^", "**")
    value = value.replace("{", "(").replace("}", ")")
    value = value.replace("≤", "<=").replace("≥", ">=")
    value = re.sub(r"\\frac\(([^()]*)\)\(([^()]*)\)", r"(\1)/(\2)", value)
    value = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1)/(\2)", value)
    value = re.sub(r"\\([a-zA-Z]+)", r"\1", value)
    return value


def _expression(value: str, aliases: dict[str, str] | None = None) -> Any:
    value = _clean(value)
    aliases = aliases or {}
    for source, target in aliases.items():
        value = re.sub(rf"\b{re.escape(source)}\b", target, value)
    if "=" in value and not any(op in value for op in [">=", "<="]):
        left, right = value.split("=", 1)
        return simplify(parse_expr(left, transformations=TRANSFORMATIONS) - parse_expr(right, transformations=TRANSFORMATIONS))
    return simplify(parse_expr(value, transformations=TRANSFORMATIONS))


def extract_formulas(answer: str) -> list[str]:
    found: list[str] = []
    for match in FORMULA_RE.finditer(answer):
        value = next((group for group in match.groups() if group), "").strip()
        if any(char in value for char in "=<>^"):
            found.append(value)
    return list(dict.fromkeys(found))


def validate_formula(question: str, answer: str, evidence: list[Evidence]) -> ValidationResult:
    reference_items = [e for e in evidence if e.formulae or e.material_type == "formula"]
    answer_formulas = extract_formulas(answer)
    if not reference_items and not answer_formulas:
        return ValidationResult("formula", "not_applicable", "当前回答和证据中没有可校验公式")
    if not answer_formulas:
        return ValidationResult("formula", "unknown", "回答中没有提取到公式", evidence_ids=[e.material_id for e in reference_items[:3]])
    if not reference_items:
        return ValidationResult("formula", "unknown", "回答含有公式，但检索证据没有规范公式", claim=answer_formulas[0])

    errors: list[str] = []
    checked = 0
    for answer_formula in answer_formulas:
        try:
            answer_expr = _expression(answer_formula)
        except Exception as exc:
            errors.append(f"无法解析回答公式“{answer_formula}”：{exc.__class__.__name__}")
            continue
        matched = False
        for evidence in reference_items:
            for raw_reference in evidence.formulae or [evidence.text]:
                candidates = [raw_reference]
                if raw_reference not in candidates:
                    candidates.append(raw_reference)
                for reference_formula in candidates:
                    try:
                        aliases = (evidence.metadata or {}).get("variable_aliases", {})
                        if simplify(answer_expr - _expression(reference_formula, aliases)) == 0:
                            matched = True
                            checked += 1
                            break
                    except Exception:
                        continue
                if matched:
                    break
            if matched:
                break
        if not matched:
            errors.append(f"回答公式“{answer_formula}”与规范公式不等价")

    evidence_ids = [item.material_id for item in reference_items[:3]]
    if errors:
        return ValidationResult(
            "formula",
            "invalid" if any("无法解析" in error for error in errors) else "fail",
            "；".join(errors),
            claim="；".join(answer_formulas),
            evidence_ids=evidence_ids,
            details={"answer_formulas": answer_formulas, "checked": checked},
        )
    return ValidationResult(
        "formula",
        "pass",
        "回答公式通过符号等价检查",
        claim="；".join(answer_formulas),
        evidence_ids=evidence_ids,
        details={"answer_formulas": answer_formulas, "checked": checked},
    )
