# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import contextlib
import io
import os
import stat
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.core import OrchestratorError
from github2gerrit.gerrit_rest import GerritRestClient
from github2gerrit.gerrit_rest import GerritRestError


class _DummyResp:
    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    # Support context manager
    def __enter__(self) -> _DummyResp:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


def test_gerrit_rest_parses_xssi_guard_and_json_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify GerritRestClient strips XSSI guard and parses JSON payloads.
    """

    # Force urllib code path (disable pygerrit2 usage)
    monkeypatch.setattr("github2gerrit.gerrit_rest._PygerritRestApi", None, raising=True)

    # Avoid sleeping on retries (not needed in this test) - now done by external_api framework
    monkeypatch.setattr("time.sleep", lambda s: None, raising=False)

    # Prepare a response with XSSI guard
    payload = b""")]}'
[
  {
    "_number": 42,
    "current_revision": "abcdef0123456789abcdef0123456789abcdef01"
  }
]
"""
    calls: list[str] = []

    def _fake_urlopen(req: Any, timeout: float | None = None) -> _DummyResp:
        # Track the request URL and return OK response
        url = getattr(req, "full_url", getattr(req, "get_full_url", lambda: "")())
        calls.append(str(url))
        return _DummyResp(status=200, payload=payload)

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.urllib.request.urlopen",
        _fake_urlopen,
        raising=True,
    )

    client = GerritRestClient(base_url="https://gerrit.example.org/")
    result = client.get("/changes/?q=limit:1&n=1&o=CURRENT_REVISION")

    assert isinstance(result, list)
    assert result and isinstance(result[0], dict)
    assert result[0]["_number"] == 42
    assert "changes" in calls[0]


def test_gerrit_rest_retries_on_http_503_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify retry/backoff on HTTP 503 and success on a subsequent attempt.
    """

    # Force urllib path and stub sleep
    monkeypatch.setattr("github2gerrit.gerrit_rest._PygerritRestApi", None, raising=True)
    monkeypatch.setattr("time.sleep", lambda s: None, raising=False)

    import urllib.error

    # Prepare sequence: 503 error twice, then 200 OK
    attempts: dict[str, int] = {"n": 0}

    def _http_error(code: int) -> urllib.error.HTTPError:
        headers: Message[str, str] = Message()
        return urllib.error.HTTPError(
            url="https://gerrit.example.org/changes/?q=limit:1",
            code=code,
            msg="Service Unavailable",
            hdrs=headers,
            fp=io.BytesIO(b""),
        )

    ok_payload = b'[{"_number": 7}]'

    def _fake_urlopen(req: Any, timeout: float | None = None) -> _DummyResp:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _http_error(503)
        return _DummyResp(status=200, payload=ok_payload)

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.urllib.request.urlopen",
        _fake_urlopen,
        raising=True,
    )

    client = GerritRestClient(
        base_url="https://gerrit.example.org/",
        timeout=0.1,
        max_attempts=5,
    )
    result = client.get("/changes/?q=limit:1&n=1")
    assert isinstance(result, list)
    assert result[0]["_number"] == 7
    assert attempts["n"] == 3  # two failures + one success


def test_gerrit_rest_retries_on_urlerror_timeout_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify retry behavior on socket timeout and final failure after attempts.
    """

    # Force urllib path and disable sleep
    monkeypatch.setattr("github2gerrit.gerrit_rest._PygerritRestApi", None, raising=True)
    monkeypatch.setattr("time.sleep", lambda s: None, raising=False)

    import urllib.error

    attempts: dict[str, int] = {"n": 0}

    def _fake_urlopen(req: Any, timeout: float | None = None) -> _DummyResp:
        attempts["n"] += 1
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.urllib.request.urlopen",
        _fake_urlopen,
        raising=True,
    )

    client = GerritRestClient(
        base_url="https://gerrit.example.org/",
        timeout=0.01,
        max_attempts=3,
    )
    with pytest.raises(GerritRestError):
        client.get("/changes/?q=limit:1&n=1")
    assert attempts["n"] == 3


def _mock_run_cmd_writer(
    content_provider: Callable[[Path], bytes],
) -> Callable[..., Any]:
    """
    Create a fake run_cmd that writes content to the commit-msg path and
    returns an object with stdout (HTTP code) and returncode.
    """

    class _Res:
        def __init__(self, code: int = 200) -> None:
            self.returncode = 0
            self.stdout = str(code)

    def _run_cmd(cmd: list[str], cwd: Path | str | None = None, **_: Any) -> Any:
        # The core passes cwd=self.workspace; construct hook path deterministically
        base = Path(cwd or ".")
        hook_path = base / ".git" / "hooks" / "commit-msg"
        # Ensure directory exists (in case core hasn't yet)
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        data = content_provider(hook_path)
        hook_path.write_bytes(data)
        return _Res(200)

    return _run_cmd


def test_commit_msg_hook_integrity_failure_due_to_small_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Ensure _install_commit_msg_hook rejects tiny downloads lacking content.
    """
    ws = tmp_path / "repo"
    (ws / ".git").mkdir(parents=True, exist_ok=True)
    orch = Orchestrator(workspace=ws)
    gerrit = GerritInfo(host="gerrit.example.org", port=29418, project="any")

    # Mock curl_download to write a tiny payload (below 128 bytes) and missing shebang
    def mock_curl_download(url: str, output_path: str, **kwargs: Any) -> tuple[int, str]:
        Path(output_path).write_bytes(b"echo not a hook\n")
        return (0, "200")

    monkeypatch.setattr("github2gerrit.external_api.curl_download", mock_curl_download, raising=True)

    with pytest.raises(OrchestratorError) as ei:
        orch._install_commit_msg_hook(gerrit)
    msg = str(ei.value)
    assert "size outside expected bounds" in msg or "missing shebang" in msg


def test_commit_msg_hook_integrity_success_and_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Ensure _install_commit_msg_hook accepts valid content and makes it exec.
    """
    ws = tmp_path / "repo"
    (ws / ".git").mkdir(parents=True, exist_ok=True)
    orch = Orchestrator(workspace=ws)
    gerrit = GerritInfo(host="gerrit.example.org", port=29418, project="any")

    # Mock curl_download to write a realistic payload with shebang and recognizable markers
    def mock_curl_download(url: str, output_path: str, **kwargs: Any) -> tuple[int, str]:
        lines = [
            "#!/bin/sh\n",
            "# Gerrit Code Review commit-msg hook\n",
            "add_change_id() {\n",
            "  echo 'Change-Id: Iabcdef0123456789abcdef0123456789abcdef0'\n",
            "}\n",
        ]
        # Ensure size > 128 bytes
        body = "".join(lines) + ("#" * 256) + "\n"
        Path(output_path).write_bytes(body.encode("utf-8"))
        return (0, "200")

    monkeypatch.setattr("github2gerrit.external_api.curl_download", mock_curl_download, raising=True)

    orch._install_commit_msg_hook(gerrit)

    hook_path = ws / ".git" / "hooks" / "commit-msg"
    assert hook_path.exists()
    mode = hook_path.stat().st_mode
    # Check some executable bit is set
    assert mode & stat.S_IXUSR or mode & stat.S_IXGRP or mode & stat.S_IXOTH

    # Validate contents include markers
    text = hook_path.read_text(encoding="utf-8")
    assert text.startswith("#!")
    assert "Gerrit Code Review" in text or "Change-Id" in text

    # Cleanup not strictly necessary, but keep tmp tidy
    with contextlib.suppress(OSError):
        os.remove(hook_path)
