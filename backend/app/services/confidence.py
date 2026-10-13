from __future__ import annotations

from .validators.types import ValidationResult

_LABELS = {"text": "文本", "formula": "公式", "numeric": "数值", "cross_modal": "跨模态"}
_CONTRADICTIONS = {
    "text": "文本结论与审核依据存在矛盾",
    "formula": "公式与同一关系的审核依据存在矛盾",
    "numeric": "数值或单位与依据不一致",
    "cross_modal": "文本与图表或信号证据不一致",
}


def calculate_confidence(results: list[ValidationResult], evidence_count: int, verified_evidence_count: int | None = None) -> dict:
    reasons: list[str] = []
    score = 0.45
    if evidence_count:
        score += min(0.25, evidence_count * 0.05)
        if verified_evidence_count is not None and evidence_count > verified_evidence_count:
            reasons.append("检索证据包含尚未完成教材审核的候选内容")
            score -= 0.1
    else:
        reasons.append("没有检索到可定位资料依据")
        score -= 0.3
    for result in results:
        if result.status == "pass":
            score += 0.1
        elif result.status in {"fail", "conflict"}:
            score -= 0.25
            label = _LABELS.get(result.category, "内容")
            reasons.append(_CONTRADICTIONS.get(result.category, f"{label}与依据存在矛盾"))
        elif result.status in {"unknown", "invalid"}:
            score -= 0.1
            label = _LABELS.get(result.category, "内容")
            reasons.append(f"{label}暂无法核验：缺少可比依据或超出检查范围")
    score = max(0.0, min(1.0, round(score, 3)))
    if score >= 0.75:
        level = "high"
    elif score >= 0.45:
        level = "medium"
    else:
        level = "low"
    if not reasons:
        reasons.append("有可定位证据且未发现确定性冲突")
    return {"level": level, "score": score, "reasons": list(dict.fromkeys(reasons))}
