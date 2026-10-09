from __future__ import annotations

from collections import Counter

from backend.evaluation.runner import load_cases


def test_cases_are_unique_and_cover_courses_topics_and_labels():
    cases = load_cases()
    assert len(cases) >= 120
    assert len({(case["question"], case["answer"]) for case in cases}) == len(cases)
    assert len({case["course_id"] for case in cases}) >= 3
    assert len({case["topic_id"] for case in cases}) >= 20
    assert {case["label"] for case in cases} == {"positive", "negative"}
    assert {case["category"] for case in cases} >= {"text", "formula", "numeric", "cross_modal"}
    assert all(case.get("expected_evidence_theme") for case in cases)


def test_cases_have_positive_and_negative_counts_per_category():
    cases = load_cases()
    labels = Counter((case["category"], case["label"]) for case in cases)
    for category in {case["category"] for case in cases}:
        assert labels[(category, "negative")] > 0
        assert labels[(category, "positive")] > 0
