import pytest

from backend.app.services.confidence import calculate_confidence
from backend.app.services.validators.cross_modal_validator import validate_cross_modal
from backend.app.services.validators.formula_validator import extract_formulas, validate_formula
from backend.app.services.validators.numeric_validator import validate_numeric
from backend.app.services.validators.text_validator import validate_text
from backend.app.services.validators.types import Evidence, ValidationResult


def evidence(text="采样频率至少为信号最高频率的两倍，否则会产生混叠。", formulae=None, metadata=None):
    return [Evidence(1, 1, "数字信号处理", "采样定理", "采样定理", "formula", 48, "采样定理", "seed:1", text, 3, True, metadata or {"expected_values": [{"value": 4, "unit": "kHz"}], "modality": {"frequency_hz": 1000}}, formulae or ["fmax = fs / 2"])]


def test_formula_symbolic_equivalence():
    result = validate_formula("", "fmax = 0.5 * fs", evidence())
    assert result.status == "pass"


def test_formula_non_equivalence():
    result = validate_formula("", "fmax = 2 * fs", evidence())
    assert result.status == "fail"


def test_formula_candidate_without_parseable_reference_is_unknown():
    result = validate_formula(
        "",
        "f_s > 2 f_h",
        [Evidence(2, 2, "数字信号处理", None, "页面", "formula", 58, "", "doc:1:p58", "采样频率大于两倍最高频率。", 1, False, {}, [])],
    )
    assert result.status == "unknown"

def test_numeric_unit_conversion():
    result = validate_numeric("", "结果为4000 Hz", evidence())
    assert result.status == "pass"


def test_numeric_mismatch():
    result = validate_numeric("", "结果为8 kHz", evidence())
    assert result.status == "fail"


def test_text_negation_conflict():
    result = validate_text("", "采样频率低于两倍最高频率不会产生混叠。", evidence())
    assert result.status == "fail"


def test_text_conditional_negation_is_not_marked_as_contradiction():
    result = validate_text("", "只有采样频率大于两倍最高频率时不会产生混叠，否则会产生混叠。", evidence())
    assert result.status == "pass"

def test_cross_modal_conflict():
    result = validate_cross_modal("", "频率6000 Hz", evidence())
    assert result.status == "conflict"

def test_no_evidence_boundaries_are_explicit():
    assert validate_text("", "任意概念说明。", []).status == "unknown"
    assert validate_formula("", "fmax = fs / 2", []).status == "unknown"
    assert validate_numeric("", "没有数值结论", []).status == "not_applicable"
    assert validate_cross_modal("", "主频为1000 Hz", []).status == "not_applicable"


def test_numeric_missing_unit_is_not_treated_as_pass():
    assert validate_numeric("", "结果为4", evidence()).status == "missing_unit"


def test_meaningful_concept_contradiction_is_detected():
    result = validate_text("", "采样频率低于两倍最高频率不会产生混叠。", evidence())
    assert result.status == "fail"
    assert result.evidence_ids


def _formula_evidence(reference=None, *, metadata=None, verified=True, material_type="formula"):
    return [Evidence(
        10, 10, "电路与信号", None, "审核公式", material_type, 58, "", "doc:1:p58",
        "", 1, verified, metadata or {}, [reference] if reference is not None else [],
    )]


@pytest.mark.parametrize("opening,closing", [("$", "$"), ("$$", "$$"), (r"\(", r"\)"), (r"\[", r"\]")])
def test_math_delimiters_preserve_full_fraction_without_duplicate_fragments(opening, closing):
    formula = r"f_c=\frac{1}{2\pi RC}"
    answer = f"截止频率：{opening}{formula}{closing}。因此满足电路条件。"
    assert extract_formulas(answer) == [formula]
    assert validate_formula("", answer, _formula_evidence("fc=1/(2*pi*R*C)")).status == "pass"


def test_plain_formulas_exclude_surrounding_prose_and_punctuation():
    assert extract_formulas("The relation is fmax = fs / 2, where fs is the sampling frequency.") == ["fmax = fs / 2"]
    assert extract_formulas("采样条件 f_s > 2 f_h；用于避免混叠。") == ["f_s > 2 f_h"]
    assert extract_formulas("普通描述，不是公式。") == []


def test_multiple_delimiters_and_duplicate_formulas_are_isolated():
    answer = r"\[f_c=\frac{1}{2\pi RC}\] 与 \(f_s>2f_h\)。再次 $f_s>2f_h$。"
    assert extract_formulas(answer) == [r"f_c=\frac{1}{2\pi RC}", "f_s>2f_h"]


@pytest.mark.parametrize("formula,reference", [
    (r"$f_{c}=\frac{1}{\frac{2}{R}}$", "fc=R/2"),
    (r"\[f_c=\frac{1}{2\pi RC}\]", r"f_{c}=1/(2*pi*R*C)"),
    (r"$y=\sin(\omega t)^2+\cos(\omega t)^2$", "y=1"),
    (r"$y=\alpha+\beta$", "y=α+β"),
    (r"$y=\Omega R$", "y=Ω*R"),
    ("2*fmax = fs", "fmax=fs/2"),
    ("-4*fmax = -2*fs", "fmax=fs/2"),
    ("fmax = fs * 0.5", "fmax=fs/2"),
    ("x = 2**3", "x=8"),
    ("x=1+1", "x=2"),
])
def test_common_math_and_constant_scaled_equations_are_equivalent(formula, reference):
    result = validate_formula("", formula, _formula_evidence(reference))
    assert result.status == "pass"
    assert result.details["checked"] == 1


@pytest.mark.parametrize("formula", ["f_h < f_s/2", r"$f_{s}>2 f_{h}$", r"\[f_s\gt 2f_h\]", "2*f_h < f_s"])
def test_sampling_inequality_equivalent_orientation(formula):
    assert validate_formula("", formula, _formula_evidence("fs>2*fh")).status == "pass"


@pytest.mark.parametrize("formula", ["f_h > f_s/2", "f_s < 2*f_h", "f_s >= 2*f_h", "f_s > 3*f_h"])
def test_sampling_inequality_wrong_direction_strictness_or_threshold_fails(formula):
    result = validate_formula("", formula, _formula_evidence("fs>2*fh"))
    assert result.status == "fail"
    assert result.details["failed"] == 1


@pytest.mark.parametrize("formula,reference", [
    (r"$f_c=\frac{1}{4\pi RC}$", "fc=1/(2*pi*R*C)"),
    ("fmax=2*fs", "fmax=fs/2"),
    ("x=3", "x=2"),
    ("x=1+2", "x=4"),
])
def test_real_comparable_formula_errors_fail(formula, reference):
    assert validate_formula("", formula, _formula_evidence(reference)).status == "fail"


@pytest.mark.parametrize("formula,reference", [
    ("f_s>2*f_h", "fc=1/(2*pi*R*C)"),
    ("y=x+1", "z=x+1"),
    ("x>2", "x=2"),
    ("x=y", r"x=\int_0^1 y(t)dt"),
])
def test_unrelated_or_unsupported_reference_is_unknown(formula, reference):
    result = validate_formula("", formula, _formula_evidence(reference))
    assert result.status == "unknown"
    assert result.details["failed"] == 0


@pytest.mark.parametrize("formula", [
    r"$f_c=\frac{1}{2\pi RC$",
    r"$x=\int_0^1 f(t)dt$",
    r"$x=\sum_{n=0}^{10}n$",
    r"$x=\frac{1}{}$",
    "$x=1/0$",
    "$x=y(t)$",
    "$x=2^10000$",
    "$x=" + "(" * 30 + "y" + ")" * 30 + "$",
])
def test_malformed_calculus_or_excessive_formulas_are_limitations(formula):
    result = validate_formula("", formula, _formula_evidence("x=y"))
    assert result.status == "unknown"
    assert result.details["failed"] == 0
    assert result.details["comparisons"][0]["reason"]


def test_unclosed_display_math_does_not_become_a_partial_equation():
    assert extract_formulas(r"\[fc=1/(2*pi*R*C)") == []


def test_standalone_variable_mentions_are_not_extra_formula_checks():
    answer = r"电阻 $R$ 和电容 $C$ 决定截止频率：\[f_c=\frac{1}{2\pi RC}\]"
    assert validate_formula("", answer, _formula_evidence("fc=1/(2*pi*R*C)")).status == "pass"


def test_variable_aliases_apply_to_both_answer_and_reference():
    result = validate_formula("", "f_s>2*f_h", _formula_evidence(
        "fs>2*fmax", metadata={"variable_aliases": {"f_{h}": "fmax"}},
    ))
    assert result.status == "pass"


def test_verified_reviewed_source_and_conditions_work_without_formula_rows():
    conditions = "R>0，C>0"
    result = validate_formula("", r"\[f_c=\frac{1}{2\pi RC}\]", _formula_evidence(
        metadata={"reviewed_formulas": [{"source": "fc=1/(2*pi*R*C)", "conditions": conditions}]},
        material_type="text",
    ))
    assert result.status == "pass"
    reference = result.details["comparisons"][0]["references"][0]
    assert reference["material_id"] == 10
    assert reference["conditions"] == conditions


def test_unverified_document_formulas_and_review_metadata_are_not_trustworthy():
    result = validate_formula("", "f_s>2*f_h", _formula_evidence(
        "fs>2*fh", verified=False,
        metadata={"reviewed_formulas": [{"source": "fs>2*fh", "conditions": ""}]},
    ))
    assert result.status == "unknown"
    assert not result.details["comparisons"][0]["references"]


def test_canonical_formula_rows_take_priority_over_review_metadata():
    result = validate_formula("", "x=2", _formula_evidence(
        "x=3", metadata={"reviewed_formulas": [{"source": "x=2", "conditions": ""}]},
    ))
    assert result.status == "fail"


def test_partial_check_remains_unknown_and_keeps_individual_outcomes():
    result = validate_formula("", "$x=2$；$y=z+1$", _formula_evidence("x=2"))
    assert result.status == "unknown"
    assert [item["status"] for item in result.details["comparisons"]] == ["pass", "unknown"]
    assert result.details["checked"] == 1


def test_conflicting_reviewed_references_are_not_a_fabricated_pass():
    items = _formula_evidence(metadata={"reviewed_formulas": [
        {"source": "x=2", "conditions": "condition A"},
        {"source": "x=3", "conditions": "condition B"},
    ]})
    assert validate_formula("", "$x=2$", items).status == "unknown"


def test_inequality_with_symbolic_scaling_sign_is_unknown():
    assert validate_formula("", "$R*x>2*R$", _formula_evidence("x>2")).status == "unknown"


@pytest.mark.parametrize("payload", [
    "__import__('os').system('echo unsafe')",
    "(1).__class__.__bases__",
    "Symbol('x')",
    "exec('unsafe')",
    "open('unsafe','w')",
])
def test_untrusted_formula_code_is_never_executed(payload, monkeypatch):
    import os
    calls = []
    monkeypatch.setattr(os, "system", lambda *args: calls.append(args))
    result = validate_formula("", f"$x={payload}$", _formula_evidence("x=2"))
    assert result.status == "unknown"
    assert calls == []
    assert result.details["failed"] == 0


def test_excessive_formula_length_and_count_do_not_silently_pass():
    result = validate_formula("", "$x=" + "1+" * 600 + "1$", _formula_evidence("x=2"))
    assert result.status == "unknown"
    many = "；".join(f"$x={number}$" for number in range(50))
    result = validate_formula("", many, _formula_evidence("z=1"))
    assert result.status == "unknown"
    assert result.details["limited"] > 0


def test_confidence_distinguishes_unchecked_formula_from_contradiction():
    unchecked = calculate_confidence([ValidationResult("formula", "unknown", "unparsed")], 3, 3)
    contradicted = calculate_confidence([ValidationResult("formula", "fail", "mismatch")], 3, 3)
    assert unchecked["score"] > contradicted["score"]
    assert unchecked["reasons"] != contradicted["reasons"]


def test_confidence_does_not_repeat_raw_formula_or_parser_errors():
    raw = "x=" + "y+" * 100 + "TokenError/SyntaxError"
    confidence = calculate_confidence([ValidationResult("formula", "invalid", raw)], 3, 3)
    assert all(len(reason) < 80 for reason in confidence["reasons"])
    assert all(raw not in reason for reason in confidence["reasons"])


def test_screenshot_formulas_can_pass_together_without_cross_comparison_failures():
    references = _formula_evidence("fc=1/(2*pi*R*C)") + _formula_evidence("fs>2*fh")
    answer = r"截止频率 \[f_c=\frac{1}{2\pi RC}\]。采样要求 \(f_s>2f_h\)。"
    result = validate_formula("", answer, references)
    assert result.status == "pass"
    assert result.details["checked"] == 2
    assert result.details["failed"] == 0


def test_equation_subject_can_be_a_single_letter_variable():
    assert validate_formula("", "a=b/2", _formula_evidence("2*a=b")).status == "pass"
