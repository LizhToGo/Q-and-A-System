from types import SimpleNamespace

from backend.evaluation.runner import _valid_traceability


def _evidence(material_id: int):
    return SimpleNamespace(material_id=material_id)


def _link(material_id: int, claim: str):
    return {"claim": claim, "evidence": {"material_id": material_id}}


def test_traceability_requires_claimed_links_for_all_emitted_links():
    evidence = [_evidence(1)]
    assert _valid_traceability(evidence, {"status": "traced", "links": [_link(1, "采样条件")]})
    assert not _valid_traceability(evidence, {"status": "traced", "links": []})
    assert not _valid_traceability(evidence, {"status": "traced", "links": [_link(1, "采样条件"), _link(1, "")]})
    assert not _valid_traceability(evidence, {"status": "traced", "links": [_link(1, "采样条件"), _link(2, "另一个主张")]})
