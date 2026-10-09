from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.db import SessionLocal, create_tables
from backend.evaluation.runner import load_cases, run_cases


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=Path("evaluation-report.json"))
    args = parser.parse_args()
    create_tables()
    db = SessionLocal()
    try:
        report = run_cases(db, load_cases(args.cases) if args.cases else None)
    finally:
        db.close()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["case_count", "accuracy", "traceability_rate", "conflict_accuracy", "false_positive_rate"]}, ensure_ascii=False, indent=2))
