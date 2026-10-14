from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import SessionLocal
from backend.app.services.ingestion.service import sha256_path
from backend.app.models import ExtractedItem, Formula, IngestionJob, Material, MaterialChunk, SourceDocument

ROOT = Path(__file__).resolve().parents[1]


def snapshot_rag(db: Session) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    materials = list(db.scalars(select(Material).order_by(Material.id)))
    formulas = list(db.scalars(select(Formula).order_by(Formula.id)))
    chunks = list(db.scalars(select(MaterialChunk).order_by(MaterialChunk.id)))
    documents = {document.id: document for document in db.scalars(select(SourceDocument))}
    items = {(job.source_document_id, item.page_number): item for item, job in db.execute(select(ExtractedItem, IngestionJob).join(IngestionJob, IngestionJob.id == ExtractedItem.ingestion_job_id)).all()}
    findings: list[dict[str, Any]] = []
    sidecars: list[dict[str, Any]] = []
    raw_hashes = {f"{doc}:{page}": hashlib.sha256(item.raw_text.encode('utf-8')).hexdigest() for (doc, page), item in items.items()}
    for document in documents.values():
        try:
            if sha256_path(Path(document.path)) != document.file_hash:
                findings.append({'kind': 'original_file_hash_mismatch', 'document_id': document.id})
        except OSError:
            findings.append({'kind': 'original_file_unavailable', 'document_id': document.id})
    for material in materials:
        metadata = material.metadata_json or {}
        item = items.get((material.source_document_id, material.page_number))
        path_value = metadata.get('review_manifest_file')
        if not path_value:
            continue
        path = (ROOT / str(path_value)).resolve()
        if not path.is_relative_to(ROOT / 'data') or path.suffix != '.json':
            findings.append({'kind': 'invalid_review_path', 'material_id': material.id})
            continue
        try:
            review = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            findings.append({'kind': 'unreadable_review', 'material_id': material.id, 'path': str(path.relative_to(ROOT)), 'error': exc.__class__.__name__})
            continue
        sidecars.append({'page': material.page_number, 'document_id': material.source_document_id, 'path': str(path.relative_to(ROOT)), 'review': review})
        document = documents.get(material.source_document_id)
        if review.get('source_file_sha256') != (document.file_hash if document else None) or not item or review.get('source_text_sha256') != raw_hashes[f"{material.source_document_id}:{material.page_number}"]:
            findings.append({'kind': 'source_hash_mismatch', 'material_id': material.id, 'page': material.page_number})
        if review.get('normalized_text', '').strip() != material.content.strip():
            findings.append({'kind': 'review_material_out_of_sync', 'material_id': material.id, 'page': material.page_number})
        if item and item.normalized_text.strip() != material.content.strip():
            findings.append({'kind': 'extracted_material_out_of_sync', 'material_id': material.id, 'page': material.page_number})
    return {
        'materials': [{key: getattr(row, key) for key in ('id', 'source_document_id', 'page_number', 'content', 'metadata_json', 'source_anchor', 'verified')} for row in materials],
        'formulas': [{key: getattr(row, key) for key in ('id', 'material_id', 'raw_formula', 'canonical_expression')} for row in formulas],
        'chunks': [{key: getattr(row, key) for key in ('id', 'material_id', 'chunk_index', 'text')} for row in chunks],
        'sidecars': sidecars,
        'source_hashes': {str(doc.id): doc.file_hash for doc in documents.values()},
        'raw_text_hashes': raw_hashes,
    }, findings


def run_audit(output: Path) -> dict[str, Any]:
    with SessionLocal() as db:
        snapshot, source_findings = snapshot_rag(db)
    with tempfile.TemporaryDirectory(prefix='rag-math-audit-') as folder:
        input_path = Path(folder) / 'input.json'
        rendered_path = Path(folder) / 'rendered.json'
        input_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        # Match the deployed static renderer; development-only hydration checks
        # are not applicable to this deliberately non-hydrated HTML audit.
        subprocess.run(['node', str(ROOT / 'frontend/scripts/audit-rag-math.mjs'), str(input_path), str(rendered_path)], cwd=ROOT, check=True, env={**os.environ, 'NODE_ENV': 'production'})
        report = json.loads(rendered_path.read_text(encoding='utf-8'))
    report['source_findings'] = source_findings
    report['source_hashes'] = snapshot['source_hashes']
    report['raw_text_hashes'] = snapshot['raw_text_hashes']
    report['review_sidecars'] = len(snapshot['sidecars'])
    report['scope'] = 'All stored materials rendered through EvidenceView formatting and actual RichText/Streamdown SSR, plus isolated syntax checks for chunks, canonical records and linked review formulas. Semantic MathML and GFM table-boundary checks do not prove textbook mathematical truth or browser layout correctness.'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description='全量检查 RAG 数学展示、索引分块及审核来源一致性')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/rag-writing-audit.json')
    parser.add_argument('--allow-findings', action='store_true', help='保留完整问题报告但不返回失败退出码（用于修复前审计）')
    args = parser.parse_args()
    report = run_audit(args.output)
    print(json.dumps({'counts': report['counts'], 'by_kind': report['by_kind'], 'source_findings': len(report['source_findings']), 'review_sidecars': report['review_sidecars'], 'output': str(args.output)}, ensure_ascii=False))
    if not args.allow_findings and (report['findings'] or report['source_findings']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
