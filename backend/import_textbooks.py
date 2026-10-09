from __future__ import annotations

import argparse
from pathlib import Path

from backend.app.db import SessionLocal, create_tables
from backend.app.services.ingestion.service import create_ingestion_job, ocr_job_pages


def _parse_pages(value: str) -> list[int]:
    pages: list[int] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            page = int(token)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"页码无效: {token}") from exc
        if page < 1:
            raise argparse.ArgumentTypeError("页码必须从 1 开始")
        pages.append(page)
    if not pages:
        raise argparse.ArgumentTypeError("至少指定一个页码")
    return pages


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="把教材文件解析为待审核知识候选（默认不运行 OCR）")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--course-id", type=int, default=None)
    parser.add_argument("--ocr-pages", type=_parse_pages, default=None, help="显式 OCR 指定页码，例如 1,3,8；不指定则保持快速原生导入")
    args = parser.parse_args()
    create_tables()
    db = SessionLocal()
    try:
        for path in args.paths:
            job = create_ingestion_job(db, path, args.course_id)
            ocr_count = 0
            if args.ocr_pages:
                ocr_count = len(ocr_job_pages(db, job.id, args.ocr_pages))
            print(f"{path.name}: job={job.id} status={job.status} pages={job.total_pages} ocr={ocr_count} summary={job.summary}")
    finally:
        db.close()
