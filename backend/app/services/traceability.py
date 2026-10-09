from __future__ import annotations

from .retrieval import evidence_dict
from .validators.types import Evidence, ValidationResult


def build_traceability(evidence: list[Evidence], results: list[ValidationResult]) -> dict:
    evidence_map = {item.material_id: item for item in evidence}
    links: list[dict] = []
    for result in results:
        for material_id in result.evidence_ids:
            item = evidence_map.get(material_id)
            if item:
                links.append({"claim": result.claim, "relation": "supports" if result.status == "pass" else "checks", "evidence": evidence_dict(item)})
    unique: dict[tuple[str, int], dict] = {}
    for link in links:
        unique[(link["claim"], link["evidence"]["material_id"])] = link
    return {
        "status": "traced" if evidence else "no_evidence",
        "message": "已关联教材资料" if evidence else "无可定位依据",
        "links": list(unique.values()),
        "evidence_count": len(evidence),
    }
