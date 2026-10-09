from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.app.services.agent import ValidationAgent


CASES_PATH = Path(__file__).resolve().parent / "cases.jsonl"
UNKNOWN_STATUSES = {"unknown", "not_applicable"}
ERROR_STATUSES = {"fail", "conflict", "invalid", "missing_unit", "dimension_error", "mismatch"}


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _empty_stats() -> dict[str, Any]:
    return {
        "total": 0,
        "known_total": 0,
        "correct": 0,
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "tn": 0,
        "unknown": 0,
        "positive_label": "error",
        "accuracy": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "false_positive_rate": 0.0,
    }


def _finish_stats(bucket: dict[str, Any]) -> None:
    known = bucket["tp"] + bucket["fp"] + bucket["fn"] + bucket["tn"]
    bucket["known_total"] = known
    bucket["accuracy"] = round((bucket["tp"] + bucket["tn"]) / known, 4) if known else 0.0
    bucket["precision"] = round(bucket["tp"] / (bucket["tp"] + bucket["fp"]), 4) if bucket["tp"] + bucket["fp"] else 0.0
    bucket["recall"] = round(bucket["tp"] / (bucket["tp"] + bucket["fn"]), 4) if bucket["tp"] + bucket["fn"] else 0.0
    bucket["false_positive_rate"] = round(bucket["fp"] / (bucket["fp"] + bucket["tn"]), 4) if bucket["fp"] + bucket["tn"] else 0.0


def _valid_traceability(evidence: list[Any], traceability: dict[str, Any]) -> bool:
    """Require every emitted link to point to retrieved evidence and carry a claim."""
    evidence_ids = {item.material_id for item in evidence}
    links = traceability.get("links") or []
    if not evidence_ids or traceability.get("status") != "traced" or not links:
        return False
    return all(
        link.get("evidence", {}).get("material_id") in evidence_ids
        and bool(str(link.get("claim") or "").strip())
        for link in links
    )


def run_cases(db: Session, cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cases = cases if cases is not None else load_cases()
    rows: list[dict[str, Any]] = []
    traced = 0
    conflict_tp = conflict_fp = conflict_fn = conflict_tn = conflict_unknown = 0
    stats: dict[str, dict[str, Any]] = {}
    for case in cases:
        evidence, results, confidence, traceability, trace = ValidationAgent(db).run(
            case["question"], case["answer"], case.get("course_id"), case.get("topic_id")
        )
        category = case["expected_category"]
        category_result = next((result for result in results if result.category == category), None)
        predicted = category_result.status if category_result else "unknown"
        expected = case["expected_status"]
        correct = predicted == expected
        bucket = stats.setdefault(category, _empty_stats())
        bucket["total"] += 1
        bucket["correct"] += int(correct)

        expected_error = expected != "pass"
        predicted_unknown = predicted in UNKNOWN_STATUSES
        predicted_error = predicted in ERROR_STATUSES
        if predicted_unknown:
            bucket["unknown"] += 1
        elif expected_error and predicted_error:
            bucket["tp"] += 1
        elif expected_error:
            bucket["fn"] += 1
        elif predicted_error:
            bucket["fp"] += 1
        else:
            bucket["tn"] += 1

        expected_conflict = case.get("error_type") == "wrong_value_and_spectrum"
        predicted_conflict = any(result.status == "conflict" for result in results)
        if predicted in UNKNOWN_STATUSES:
            conflict_unknown += 1
        elif expected_conflict and predicted_conflict:
            conflict_tp += 1
        elif expected_conflict:
            conflict_fn += 1
        elif predicted_conflict:
            conflict_fp += 1
        else:
            conflict_tn += 1

        trace_valid = _valid_traceability(evidence, traceability)
        traced += int(trace_valid)
        rows.append({
            "case_id": case["case_id"],
            "expected": expected,
            "predicted": predicted,
            "correct": correct,
            "evidence_count": len(evidence),
            "traceability_valid": trace_valid,
            "confidence": confidence,
            "trace": trace,
        })

    for category, bucket in stats.items():
        bucket["positive_error_count"] = sum(case["expected_category"] == category and case.get("expected_status") != "pass" for case in cases)
        bucket["negative_clean_count"] = sum(case["expected_category"] == category and case.get("expected_status") == "pass" for case in cases)
        _finish_stats(bucket)
    total = len(cases)
    dataset_courses = sorted({case.get("course_id") for case in cases if case.get("course_id") is not None})
    dataset_topics = sorted({case.get("topic_id") for case in cases if case.get("topic_id") is not None})
    positive_count = sum(case.get("expected_status") != "pass" for case in cases)
    negative_count = total - positive_count
    conflict_known = conflict_tp + conflict_fp + conflict_fn + conflict_tn
    false_positive_denominator = conflict_fp + conflict_tn
    return {
        "case_count": total,
        "dataset_coverage": {
            "course_ids": dataset_courses,
            "topic_ids": dataset_topics,
            "course_count": len(dataset_courses),
            "topic_count": len(dataset_topics),
            "positive_error_count": positive_count,
            "negative_clean_count": negative_count,
        },
        "correct_count": sum(row["correct"] for row in rows),
        "accuracy": round(sum(row["correct"] for row in rows) / total, 4) if total else 0.0,
        "traceability_rate": round(traced / total, 4) if total else 0.0,
        "conflict_accuracy": round((conflict_tp + conflict_tn) / conflict_known, 4) if conflict_known else 0.0,
        "false_positive_rate": round(conflict_fp / false_positive_denominator, 4) if false_positive_denominator else 0.0,
        "confusion": {
            "conflict_tp": conflict_tp,
            "conflict_fp": conflict_fp,
            "conflict_fn": conflict_fn,
            "conflict_tn": conflict_tn,
            "conflict_unknown": conflict_unknown,
        },
        "category_stats": stats,
        "rows": rows,
    }
