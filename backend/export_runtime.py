from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

from backend.app.services.ingestion.service import sha256_path

ROOT_DIR = Path(__file__).resolve().parents[1]
_CONFIG_FILES = (".env", "data/provider-config.json", "data/admin-token")
_ARTIFACT_NAME = re.compile(r"[0-9a-f]{32}\.png\Z")


class RuntimeExportError(RuntimeError):
    """A credential-free, consumer-visible export failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _Output:
    def __init__(self, root: Path):
        self.root = root
        self.files: list[Path] = []
        self.directories: list[Path] = []
        self.records: list[dict[str, Any]] = []
        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            raise RuntimeExportError("output_exists", "输出目录已存在；请使用全新的目录，不会覆盖已有输出") from None
        self.directories.append(root)

    def directory(self, path: Path) -> None:
        if path in self.directories:
            return
        self.directory(path.parent)
        path.mkdir(mode=0o700)
        self.directories.append(path)

    def open(self, relative: str):
        path = self.root / relative
        self.directory(path.parent)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.files.append(path)
        return os.fdopen(descriptor, "wb")

    def record(self, relative: str) -> dict[str, Any]:
        path = self.root / relative
        record = {"path": relative, "sha256": sha256_path(path), "size_bytes": path.stat().st_size}
        self.records.append(record)
        return record

    def copy(self, source: Path, relative: str) -> dict[str, Any]:
        with source.open("rb") as reader, self.open(relative) as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
        return self.record(relative)

    def cleanup(self) -> None:
        # Never recursively remove an output directory: concurrent/unrelated files
        # are not ours, even if they appeared after we exclusively created it.
        for path in reversed(self.files):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        for path in reversed(self.directories):
            try:
                path.rmdir()
            except OSError:
                pass


def _review_files(snapshot: sqlite3.Connection, root: Path) -> list[tuple[Path, str]]:
    references: set[str] = set()
    for (raw_metadata,) in snapshot.execute("SELECT metadata_json FROM materials"):
        try:
            metadata = json.loads(raw_metadata) if raw_metadata else {}
        except (TypeError, ValueError):
            raise RuntimeExportError("invalid_metadata", "教材审核元数据不是有效 JSON") from None
        if not isinstance(metadata, dict):
            raise RuntimeExportError("invalid_metadata", "教材审核元数据必须是 JSON 对象")
        value = metadata.get("review_manifest_file")
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise RuntimeExportError("invalid_review_path", "审核清单引用必须是 data 下的相对 JSON 路径")
        relative = PurePosixPath(value.replace("\\", "/"))
        if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2
                or relative.parts[0] != "data" or relative.suffix.lower() != ".json"):
            raise RuntimeExportError("invalid_review_path", "审核清单引用必须是 data 下的相对 JSON 路径")
        references.add(relative.as_posix())
    result = []
    data_root = (root / "data").resolve()
    private_paths = {(root / relative).resolve() for relative in _CONFIG_FILES}
    for relative in sorted(references):
        path = root / relative
        resolved = path.resolve()
        if (not resolved.is_relative_to(data_root) or resolved in private_paths
                or path.is_symlink() or resolved != path.absolute()):
            raise RuntimeExportError("unsafe_review_path", "审核清单引用不能越出数据目录或指向私密配置/符号链接")
        if not path.is_file():
            raise RuntimeExportError("missing_review_file", "数据库引用的审核清单文件不存在")
        result.append((path, relative))
    return result


def export_runtime(output: Path, *, database: Path | None = None,
                   include_config: bool = False, root: Path | None = None) -> dict[str, Any]:
    """Export a read-only SQLite backup and its runtime files to a new directory.

    ``root`` selects the application root for data sidecars/configuration and
    relative book paths; it defaults to the project root, independently of cwd.
    The returned manifest is also written last as runtime-manifest.json.
    """
    root = Path(root).resolve() if root is not None else ROOT_DIR
    database = Path(database).resolve() if database is not None else root / "data" / "knowledge.db"
    output = Path(output).absolute()
    owned: _Output | None = None
    stage = "创建输出目录"
    try:
        if not database.is_file():
            raise RuntimeExportError("missing_database", "源 SQLite 数据库不存在")
        owned = _Output(output)
        database_relative = "data/knowledge.db"
        with owned.open(database_relative):
            pass
        stage = "获取 SQLite 一致快照"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(output / database_relative)) as snapshot:
                source.backup(snapshot)
                snapshot.execute("PRAGMA journal_mode=DELETE")
                documents = []
                stage = "复制并校验教材原文件"
                for document_id, filename, expected_hash, original_path in snapshot.execute(
                        "SELECT id, filename, file_hash, path FROM source_documents ORDER BY id").fetchall():
                    if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is None:
                        raise RuntimeExportError("invalid_book_hash", f"教材 {document_id} 的 SHA-256 身份无效")
                    book = Path(original_path)
                    if not book.is_absolute():
                        book = root / book
                    if not book.is_file():
                        raise RuntimeExportError("missing_book", f"教材 {document_id} 的原文件不存在")
                    if sha256_path(book) != expected_hash.lower():
                        raise RuntimeExportError("book_hash_mismatch", f"教材 {document_id} 的原文件与数据库 SHA-256 不一致")
                    suffix = book.suffix.lower()
                    if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) is None:
                        raise RuntimeExportError("invalid_book_suffix", f"教材 {document_id} 的原文件扩展名不能移植")
                    relative = f"data/uploads/doc-{document_id}-{expected_hash.lower()}{suffix}"
                    record = owned.copy(book, relative)
                    if record["sha256"] != expected_hash.lower():
                        raise RuntimeExportError("book_changed", f"教材 {document_id} 在复制期间改变，已取消导出")
                    portable_path = "/app/" + relative
                    snapshot.execute("UPDATE source_documents SET path=? WHERE id=?", (portable_path, document_id))
                    documents.append({"id": document_id, "path": portable_path, "sha256": record["sha256"]})
                stage = "携带审核清单"
                for path, relative in _review_files(snapshot, root):
                    owned.copy(path, relative)
                snapshot.commit()
                tables = [row[0] for row in snapshot.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                counts = {table: snapshot.execute(
                    'SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0] for table in tables}
        owned.record(database_relative)
        stage = "携带历史工具 PNG"
        artifacts = root / "data" / "tool_artifacts"
        if artifacts.exists():
            if artifacts.is_symlink() or not artifacts.is_dir():
                raise RuntimeExportError("unsafe_artifacts", "工具图像目录必须是真实目录")
            for path in sorted(artifacts.iterdir()):
                if _ARTIFACT_NAME.fullmatch(path.name):
                    if path.is_symlink() or not path.is_file():
                        raise RuntimeExportError("unsafe_artifact", "已完成工具 PNG 必须是真实文件")
                    owned.copy(path, "data/tool_artifacts/" + path.name)
        stage = "携带显式授权配置"
        configuration = []
        for relative in _CONFIG_FILES:
            path = root / relative
            present = path.is_file()
            entry: dict[str, Any] = {"path": relative, "present": present, "included": include_config and present}
            if include_config and present:
                entry.update(owned.copy(path, relative))
            configuration.append(entry)
        manifest = {"schema_version": 1, "completed": True, "database": database_relative,
                    "table_counts": counts, "documents": documents, "files": owned.records,
                    "configuration": configuration}
        stage = "写入完成清单"
        with owned.open("runtime-manifest.json") as handle:
            handle.write((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return manifest
    except BaseException as error:
        if owned is not None:
            owned.cleanup()
        if isinstance(error, RuntimeExportError):
            raise
        if isinstance(error, sqlite3.Error):
            raise RuntimeExportError("database_error", f"{stage}失败；SQLite 数据库不可用或结构不匹配") from None
        if isinstance(error, OSError):
            # errno and our stage identify the local failure without emitting
            # arbitrary exception text, source paths, configuration, or keys.
            raise RuntimeExportError("filesystem_error", f"{stage}失败；文件系统错误 errno={error.errno}") from None
        if isinstance(error, (TypeError, ValueError)):
            raise RuntimeExportError("invalid_data", f"{stage}失败；运行时数据格式无效") from None
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读备份 SQLite 并导出 Linux Docker 可移植运行时，不覆盖已有输出")
    parser.add_argument("--output", type=Path, required=True, help="全新的运行时输出目录")
    parser.add_argument("--database", type=Path, help="源 SQLite 路径；默认项目 data/knowledge.db")
    parser.add_argument("--include-config", action="store_true", help="显式携带 .env、模型配置和已有管理员 token（含秘密）")
    args = parser.parse_args(argv)
    try:
        manifest = export_runtime(args.output, database=args.database, include_config=args.include_config)
    except RuntimeExportError as error:
        print(json.dumps({"completed": False, "error_code": error.code, "message": str(error)}, ensure_ascii=False), flush=True)
        return 2
    print(json.dumps({"completed": True, "documents": len(manifest["documents"]),
                      "table_counts": manifest["table_counts"], "config_included": args.include_config,
                      "manifest": "runtime-manifest.json"}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
