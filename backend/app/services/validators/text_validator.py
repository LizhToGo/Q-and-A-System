from __future__ import annotations

import re

from .types import Evidence, ValidationResult


NEGATION_PAIRS = [
    ("会产生混叠", "不会产生混叠"),
    ("产生混叠", "不产生混叠"),
    ("能够避免混叠", "不能避免混叠"),
    ("稳定", "不稳定"),
    ("因果", "非因果"),
    ("可以恢复", "不能恢复"),
    ("至少为两倍", "低于两倍"),
]


def _contains_unnegated(text: str, phrase: str) -> bool:
    for match in re.finditer(re.escape(phrase), text):
        if match.start() == 0 or text[match.start() - 1] not in "不未无没":
            return True
    return False


def _sentences(answer: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？!?;；\n]", answer) if item.strip()]


def _tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z]{2,}|\d+(?:\.\d+)?", value):
        if all("\u4e00" <= char <= "\u9fff" for char in raw):
            tokens.append(raw)
            for width in (2, 3, 4):
                tokens.extend(raw[index:index + width] for index in range(max(0, len(raw) - width + 1)))
        else:
            tokens.append(raw)
    return list(dict.fromkeys(token for token in tokens if len(token) >= 2))


def validate_text(question: str, answer: str, evidence: list[Evidence]) -> ValidationResult:
    if not evidence:
        return ValidationResult("text", "unknown", "没有检索到可用于概念判断的教材证据", claim=answer)
    combined = " ".join(item.text for item in evidence)
    normalized_answer = answer.replace(" ", "")
    normalized_source = combined.replace(" ", "")
    for source_phrase, answer_phrase in NEGATION_PAIRS:
        source_has_positive = _contains_unnegated(normalized_source, source_phrase)
        answer_has_negative = answer_phrase in normalized_answer
        answer_has_positive = _contains_unnegated(normalized_answer, source_phrase)
        if source_has_positive and answer_has_negative and not answer_has_positive:
            source_id = next(item.material_id for item in evidence if source_phrase in item.text.replace(" ", ""))
            return ValidationResult(
                "text",
                "fail",
                f"回答中的“{answer_phrase}”与教材中的“{source_phrase}”相矛盾",
                claim=answer_phrase,
                evidence_ids=[source_id],
                details={"contradiction": {"source": source_phrase, "answer": answer_phrase}},
            )
    contradiction_markers = ("相反", "不成立", "不具有", "无关", "错误")
    if any(marker in normalized_answer for marker in contradiction_markers):
        return ValidationResult("text", "fail", "回答包含否定或相反主张，需要与教材证据重新核对", claim=answer, evidence_ids=[e.material_id for e in evidence[:3]], details={"markers": [marker for marker in contradiction_markers if marker in normalized_answer]})
    meaningful = [s for s in _sentences(answer) if len(s) >= 4]
    hit_count = 0
    for sentence in meaningful:
        tokens = _tokens(sentence)
        if any(token in normalized_source for token in tokens):
            hit_count += 1
    if not meaningful or hit_count == 0:
        return ValidationResult("text", "unknown", "回答缺少教材中可匹配的概念主张", claim=answer, evidence_ids=[e.material_id for e in evidence[:3]])
    return ValidationResult(
        "text",
        "pass",
        f"找到 {len(evidence)} 条相关教材证据，概念主张存在匹配",
        claim=answer,
        evidence_ids=[e.material_id for e in evidence[:3]],
        details={"matched_sentences": hit_count, "sentence_count": len(meaningful)},
    )
