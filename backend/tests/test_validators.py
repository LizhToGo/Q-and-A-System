from backend.app.services.validators.cross_modal_validator import validate_cross_modal
from backend.app.services.validators.formula_validator import validate_formula
from backend.app.services.validators.numeric_validator import validate_numeric
from backend.app.services.validators.text_validator import validate_text
from backend.app.services.validators.types import Evidence


def evidence(text="采样频率至少为信号最高频率的两倍，否则会产生混叠。", formulae=None, metadata=None):
    return [Evidence(1, 1, "数字信号处理", "采样定理", "采样定理", "formula", 48, "采样定理", "seed:1", text, 3, True, metadata or {"expected_values": [{"value": 4, "unit": "kHz"}], "modality": {"frequency_hz": 1000}}, formulae or ["fmax = fs / 2"])]


def test_formula_symbolic_equivalence():
    result = validate_formula("", "fmax = 0.5 * fs", evidence())
    assert result.status == "pass"


def test_formula_non_equivalence():
    result = validate_formula("", "fmax = 2 * fs", evidence())
    assert result.status == "fail"


def test_numeric_unit_conversion():
    result = validate_numeric("", "结果为4000 Hz", evidence())
    assert result.status == "pass"


def test_numeric_mismatch():
    result = validate_numeric("", "结果为8 kHz", evidence())
    assert result.status == "fail"


def test_text_negation_conflict():
    result = validate_text("", "采样频率低于两倍最高频率不会产生混叠。", evidence())
    assert result.status == "fail"


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
