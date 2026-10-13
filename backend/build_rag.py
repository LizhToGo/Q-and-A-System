from __future__ import annotations

import argparse
import json

from backend.app.db import SessionLocal, create_tables
from backend.app.services.ingestion.service import build_rag_index


def main() -> None:
    parser = argparse.ArgumentParser(description="为一个教材导入任务建立完整、可追溯的 RAG 页面索引")
    parser.add_argument("--job-id", type=int, required=True, help="教材导入任务 ID")
    parser.add_argument("--course-id", type=int, default=None, help="覆盖教材关联课程 ID")
    args = parser.parse_args()

    create_tables()
    db = SessionLocal()
    try:
        result = build_rag_index(db, args.job_id, args.course_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        db.close()


if __name__ == "__main__":
    main()
