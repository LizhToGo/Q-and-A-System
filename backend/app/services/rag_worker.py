from __future__ import annotations

import atexit
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import tempfile
import threading
import weakref

from sqlalchemy import event
from sqlalchemy.engine import Engine

from ..config import settings


class RetrievalError(RuntimeError):
    """The retrieval worker failed; this is not an empty evidence result."""


_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_ENV_NAMES = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
    "DOTNET_ROOT", "DOTNET_ROOT_X64", "DOTNET_ROOT_X86", "LD_LIBRARY_PATH",
})


def worker_environment() -> dict[str, str]:
    # Allow only runtime/OS necessities, never model credentials or app settings.
    return {name: value for name, value in os.environ.items() if name.upper() in _ENV_NAMES}


class RagWorker:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.revision: str | None = None
        self._process: subprocess.Popen | None = None
        self._stderr_file = None

    def close(self) -> None:
        with self.lock:
            self.revision = None
            process, self._process = self._process, None
            if process is None:
                return
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
            if self._stderr_file is not None:
                self._stderr_file.close()
                self._stderr_file = None

    def _diagnostic(self) -> str:
        if self._stderr_file is None:
            return ""
        self._stderr_file.seek(0, os.SEEK_END)
        length = self._stderr_file.tell()
        self._stderr_file.seek(max(0, length - 4096))
        return self._stderr_file.read().decode("utf-8", errors="replace").strip()

    def _start(self) -> subprocess.Popen:
        if self._process is not None:
            if self._process.poll() is not None:
                diagnostic = self._diagnostic()
                self.close()
                raise RetrievalError(f"C# retrieval worker exited unexpectedly: {diagnostic or 'no runtime diagnostic'}")
            return self._process
        artifact = Path(settings.rag_engine_path).resolve()
        if not artifact.is_file():
            raise RetrievalError(f"C# retrieval artifact missing: {artifact}")
        try:
            self._stderr_file = tempfile.TemporaryFile()
            self._process = subprocess.Popen(
                [settings.rag_dotnet_executable, str(artifact)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_file,
                cwd=artifact.parent, env=worker_environment(), bufsize=0,
            )
        except OSError as exc:
            if self._stderr_file is not None:
                self._stderr_file.close()
                self._stderr_file = None
            raise RetrievalError("Cannot start C# retrieval worker") from exc
        self.revision = None
        return self._process

    def request(self, payload: dict) -> list[dict]:
        with self.lock:
            process = self._start()
            encoded = (json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            completed: Queue = Queue(maxsize=1)

            def exchange() -> None:
                try:
                    # Unbuffered pipes can short-write and short-read. Bound the
                    # whole exchange, including writes when a child stops reading.
                    remaining = memoryview(encoded)
                    while remaining:
                        written = process.stdin.write(remaining)
                        if not written:
                            raise OSError("Retrieval pipe closed")
                        remaining = remaining[written:]
                    parts = bytearray()
                    while len(parts) <= _MAX_RESPONSE_BYTES:
                        block = os.read(process.stdout.fileno(), min(65536, _MAX_RESPONSE_BYTES + 1 - len(parts)))
                        if not block:
                            raise OSError("Retrieval worker closed its output")
                        parts.extend(block)
                        if b"\n" in block:
                            if not parts.endswith(b"\n") or parts.count(b"\n") != 1:
                                raise ValueError("Unexpected extra retrieval output")
                            completed.put(json.loads(parts))
                            return
                    raise ValueError("Retrieval response exceeded size limit")
                except Exception as exc:
                    completed.put(exc)

            threading.Thread(target=exchange, name="rag-worker-exchange", daemon=True).start()
            try:
                response = completed.get(timeout=settings.rag_worker_timeout)
                if isinstance(response, Exception):
                    diagnostic = self._diagnostic() if process.poll() is not None else ""
                    raise RetrievalError(f"C# retrieval worker communication failed: {diagnostic or 'no runtime diagnostic'}") from response
                if not isinstance(response, dict):
                    raise RetrievalError("Invalid C# retrieval response")
                if "error" in response:
                    error = response["error"]
                    code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
                    raise RetrievalError(f"C# retrieval worker rejected the request ({code})")
                expected_revision = (payload.get("snapshot") or {}).get("revision", self.revision)
                matches = response.get("matches")
                if expected_revision is None or response.get("revision") != expected_revision or not isinstance(matches, list):
                    raise RetrievalError("C# retrieval response revision/schema mismatch")
                for match in matches:
                    if not isinstance(match, dict) or type(match.get("material_id")) is not int or type(match.get("chunk_id")) is not int:
                        raise RetrievalError("Invalid C# retrieval evidence identifiers")
                    score = match.get("score")
                    if type(score) not in (int, float) or not math.isfinite(score) or score <= 0:
                        raise RetrievalError("Invalid C# retrieval score")
                self.revision = expected_revision
                return matches
            except Empty as exc:
                self.close()
                raise RetrievalError("C# retrieval worker timed out") from exc
            except Exception:
                self.close()
                raise


_workers: weakref.WeakKeyDictionary[Engine, RagWorker] = weakref.WeakKeyDictionary()
_workers_lock = threading.Lock()


def worker_for(engine: Engine) -> RagWorker:
    with _workers_lock:
        worker = _workers.get(engine)
        if worker is None:
            worker = RagWorker()
            _workers[engine] = worker
            event.listen(engine, "engine_disposed", lambda _engine: worker.close())
            weakref.finalize(engine, worker.close)
        return worker


def close_workers() -> None:
    with _workers_lock:
        workers = list(_workers.values())
    for worker in workers:
        worker.close()


atexit.register(close_workers)
