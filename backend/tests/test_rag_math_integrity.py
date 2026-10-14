from __future__ import annotations

from backend.app.services.ingestion.service import _chunk_text, _formula_sources
from backend.app.services.retrieval import evidence_dict
from backend.app.services.validators.formula_validator import validate_formula
from backend.app.services.validators.types import Evidence


def _reference(text: str = '') -> Evidence:
    return Evidence(material_id=1, chunk_id=1, course='模拟电子技术基础', topic=None, title='截止频率', material_type='formula', page_number=1, section='', source_anchor='doc:1:p1', text=text, score=1, verified=True, formulae=['fc=1/(2*pi*R*C)'])


def test_chunk_boundary_preserves_a_formula_needed_for_verification():
    formula = r'\[f_c=\frac{1}{2\pi RC}\]'
    page = '教材说明。' * 196 + formula + '截止频率由电阻和电容确定。'
    results = [validate_formula('截止频率是什么？', chunk, [_reference()]) for chunk in _chunk_text(page, max_chars=1000, overlap_paragraphs=0)]
    assert sum(result.status == 'pass' for result in results) == 1
    assert not any(result.status in {'fail', 'invalid'} for result in results)


def test_formula_longer_than_chunk_target_is_not_split_or_discarded():
    formula = r'\[f_c=\frac{1}{2\pi RC}' + '+0' * 40 + r'\]'
    results = [validate_formula('截止频率是什么？', chunk, [_reference()]) for chunk in _chunk_text('说明。\n\n' + formula + '\n\n结束。', max_chars=40, overlap_paragraphs=0)]
    assert sum(result.status == 'pass' for result in results) == 1
    assert not any(result.status in {'fail', 'invalid'} for result in results)


def test_model_evidence_excerpt_keeps_closing_delimiter_and_full_equation():
    formula = r'\[f_c=\frac{1}{2\pi RC}' + '+0' * 20 + r'\]'
    reference = _reference('说明。' * 395 + formula + '后续补充内容。' * 20)
    response = evidence_dict(reference)
    result = validate_formula('截止频率是什么？', response['snippet'], [_reference()])
    assert result.status == 'pass'


def test_blank_lines_inside_display_math_are_not_paragraph_cuts():
    formula = r'\[f_c=' + '\n\n' + r'\frac{1}{2\pi RC}\]'
    results = [validate_formula('截止频率是什么？', chunk, [_reference()]) for chunk in _chunk_text('说明。\n\n' + formula, max_chars=12, overlap_paragraphs=0)]
    assert sum(result.status == 'pass' for result in results) == 1


def test_reviewed_correction_supersedes_stale_legacy_formula():
    payload = {'formulas': ['fc=1/(pi*R*C)'], 'reviewed_formulas': [{'source': 'fc=1/(2*pi*R*C)', 'conditions': '一阶RC网络'}]}
    evidence = _reference()
    evidence.formulae = _formula_sources(payload, verified=True)
    assert validate_formula('截止频率是什么？', r'\(f_c=\frac{1}{2\pi RC}\)', [evidence]).status == 'pass'
    assert validate_formula('截止频率是什么？', r'\(f_c=\frac{1}{\pi RC}\)', [evidence]).status == 'fail'
