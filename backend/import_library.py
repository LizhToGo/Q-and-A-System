from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from pathlib import Path

from backend.app.db import create_tables
from backend.app.services.ingestion.library_import import import_library, load_document_specs


@contextmanager
def _exclusive_output(directory: Path):
    """OS-owned lock is released even after a terminated unattended importer."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".library-import.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            import msvcrt
        except ImportError:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("输出目录已有正在运行的全书导入进程") from None
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("输出目录已有正在运行的全书导入进程") from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立导入全书全文，按页断点恢复并生成待人工审核清单")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--document-ids", nargs="+", type=int, help="数据库中的教材来源 ID")
    source.add_argument("--manifest", type=Path, help="schema_version=1、documents 数组书清单；PDF 路径仍从数据库加载")
    parser.add_argument("--output-dir", type=Path, required=True, help="检查点、版本和 library.review.json 的目录")
    parser.add_argument("--provider-id", help="模型模式临时覆盖前台默认 provider，不保存配置；ocr 不使用")
    parser.add_argument("--model", help="模型模式临时覆盖模型，不保存配置；ocr 不使用")
    parser.add_argument("--mode", choices=("auto", "text", "vision", "ocr"), default="auto",
                        help="ocr：显式完全本地解析/OCR，不加载模型配置、不联网；auto/text/vision 保持模型转写，不静默回退本地")
    parser.add_argument("--workers", type=int, default=2, help="页准备并发 1–8；OCR 串行，DB 逐页串行事务")
    parser.add_argument("--pages", nargs="+", type=int, help="仅处理明确指定的 PDF 页码，应用于每本所选书")
    parser.add_argument("--retries", type=int, default=2, help="仅模型模式传输/限流失败额外重试 0–5，遵守 Retry-After")
    parser.add_argument("--max-output-tokens", type=int, default=8192, help="仅模型模式整页输出上限1024–16384；ocr 不截断正文")
    parser.add_argument("--force", action="store_true", help="忽略成功检查点重新解析/转写为新版本；仍不覆盖人工修改/approved 页")
    args = parser.parse_args(argv)
    try:
        specs = load_document_specs(args.manifest, args.document_ids)
        create_tables()
        with _exclusive_output(args.output_dir.resolve()):
            result = import_library(specs, output_dir=args.output_dir, pages=args.pages,
                                    provider_id=args.provider_id, model=args.model, mode=args.mode,
                                    workers=args.workers, retries=args.retries, force=args.force,
                                    max_output_tokens=args.max_output_tokens,
                                    progress=lambda line: print(line, flush=True))
        print(json.dumps({"completed": result["completed"], "statistics": result["statistics"],
                          "review_manifest": result["review_manifest"]}, ensure_ascii=False), flush=True)
        return 1 if result["statistics"]["failed"] else 0
    except (ValueError, RuntimeError, OSError) as error:
        # Never print arbitrary upstream exception text: it can contain request headers.
        print(json.dumps({"completed": False, "error_type": error.__class__.__name__,
                          "message": "导入未启动或中断；检查教材身份、参数、配置与输出目录锁"}, ensure_ascii=False), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
