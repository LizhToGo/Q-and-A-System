from __future__ import annotations

from .validators.types import ValidationResult


def calculate_confidence(results: list[ValidationResult], evidence_count: int) -> dict:
    reasons: list[str] = []
    score = 0.45
    if evidence_count:
        score += min(0.25, evidence_count * 0.05)
    else:
        reasons.append("没有检索到可定位资料依据")
        score -= 0.3
    for result in results:
        if result.status == "pass":
            score += 0.1
        elif result.status in {"fail", "invalid", "conflict"}:
            score -= 0.25
            reasons.append(f"{result.category}检查{result.status}：{result.message}")
        elif result.status == "unknown":
            score -= 0.1
            reasons.append(f"{result.category}缺少足够依据")
    score = max(0.0, min(1.0, round(score, 3)))
    if score >= 0.75:
        level = "high"
    elif score >= 0.45:
        level = "medium"
    else:
        level = "low"
    if not reasons:
        reasons.append("有可定位证据且未发现确定性冲突")
    return {"level": level, "score": score, "reasons": reasons}
