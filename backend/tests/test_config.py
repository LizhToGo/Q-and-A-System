from __future__ import annotations

import os

import pytest

from backend.app.config import _load_local_env


@pytest.mark.parametrize("process_value", [None, "", "process-token"])
def test_local_key_fills_missing_or_empty_environment_without_replacing_nonempty_key(tmp_path, monkeypatch, process_value):
    env_file = tmp_path / ".env"
    env_file.write_text('LLM_API_KEY="file-token"\n', encoding="utf-8")
    if process_value is None:
        monkeypatch.delenv("LLM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("LLM_API_KEY", process_value)
    original = env_file.read_bytes()

    _load_local_env(env_file)

    assert os.environ["LLM_API_KEY"] == (process_value or "file-token")
    assert env_file.read_bytes() == original
