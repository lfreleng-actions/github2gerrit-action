# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import base64
import contextlib
import datetime
import http.server
import ipaddress
import logging
import os
import re
import socket
import ssl
import stat
import threading
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
import requests
import responses
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.core import OrchestratorError
from github2gerrit.gerrit_rest import GerritRestClient
from github2gerrit.gerrit_rest import GerritRestError


_ANY_URL = re.compile(r".*")


@pytest.fixture
def mocked_gerrit(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[responses.RequestsMock]:
    """Answer the client's requests at the Requests transport.

    Everything above the transport adapter runs for real: the client,
    python-gerrit-api, the session's auth, hooks and redirects.
    """
    monkeypatch.setattr("time.sleep", lambda s: None, raising=False)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


def test_gerrit_rest_parses_xssi_guard_and_json_success(
    mocked_gerrit: responses.RequestsMock,
) -> None:
    """
    Verify GerritRestClient strips XSSI guard and parses JSON payloads.
    """
    payload = b""")]}'
[
  {
    "_number": 42,
    "current_revision": "abcdef0123456789abcdef0123456789abcdef01"
  }
]
"""
    mocked_gerrit.add(responses.GET, _ANY_URL, body=payload)

    client = GerritRestClient(base_url="https://gerrit.example.org/")
    result = client.get("/changes/?q=limit:1&n=1&o=CURRENT_REVISION")

    assert isinstance(result, list)
    assert result and isinstance(result[0], dict)
    assert result[0]["_number"] == 42
    assert "changes" in str(mocked_gerrit.calls[0].request.url)


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (b')]}\'\r\n{"a": 1}', "application/json", {"a": 1}),
        (b")]}'\n[1, 2]", "text/plain", [1, 2]),
        (b'"3.9.1"', "application/json", "3.9.1"),
        (b")]}'\n42", "application/json", 42),
    ],
    ids=["crlf-guard", "no-json-content-type", "string", "number"],
)
def test_gerrit_rest_returns_native_json_values(
    mocked_gerrit: responses.RequestsMock,
    body: bytes,
    content_type: str,
    expected: Any,
) -> None:
    """Results are plain JSON values whatever the guard or content type.

    python-gerrit-api strips only an LF guard and returns any body
    without a JSON content type as text; the client parses both.
    """
    mocked_gerrit.add(
        responses.GET, _ANY_URL, body=body, content_type=content_type
    )

    client = GerritRestClient(
        base_url="https://gerrit.example.org/", max_attempts=1
    )

    assert client.get("/config/server/version") == expected


@pytest.mark.parametrize(
    "body",
    [b"<!doctype html><html></html>", b"", b")]}'\n"],
    ids=["html", "empty", "guard-only"],
)
def test_gerrit_rest_rejects_a_body_that_is_not_json(
    mocked_gerrit: responses.RequestsMock, body: bytes
) -> None:
    """A REST read that returns no JSON fails rather than reading as data."""
    mocked_gerrit.add(
        responses.GET, _ANY_URL, body=body, content_type="text/html"
    )

    client = GerritRestClient(
        base_url="https://gerrit.example.org/", max_attempts=1
    )

    with pytest.raises(GerritRestError, match="Failed to parse JSON"):
        client.get("/changes/?q=status:open")


def test_gerrit_rest_retries_on_http_503_then_succeeds(
    mocked_gerrit: responses.RequestsMock,
) -> None:
    """
    Verify retry/backoff on HTTP 503 and success on a subsequent attempt.
    """
    mocked_gerrit.add(responses.GET, _ANY_URL, status=503)
    mocked_gerrit.add(responses.GET, _ANY_URL, status=503)
    mocked_gerrit.add(responses.GET, _ANY_URL, body=b'[{"_number": 7}]')

    client = GerritRestClient(
        base_url="https://gerrit.example.org/",
        timeout=0.1,
        max_attempts=5,
    )
    result = client.get("/changes/?q=limit:1&n=1")
    assert isinstance(result, list)
    assert result[0]["_number"] == 7
    assert len(mocked_gerrit.calls) == 3  # two failures + one success


def test_gerrit_rest_retries_on_timeout_then_fails(
    mocked_gerrit: responses.RequestsMock,
) -> None:
    """
    Verify retry behavior on a read timeout and final failure after attempts.
    """
    mocked_gerrit.add(
        responses.GET,
        _ANY_URL,
        body=requests.exceptions.ReadTimeout("Read timed out."),
    )

    client = GerritRestClient(
        base_url="https://gerrit.example.org/",
        timeout=0.01,
        max_attempts=3,
    )
    with pytest.raises(GerritRestError) as ei:
        client.get("/changes/?q=limit:1&n=1")
    assert len(mocked_gerrit.calls) == 3
    assert isinstance(ei.value.__cause__, requests.exceptions.ReadTimeout)


@pytest.mark.parametrize(
    ("status", "attempts"),
    [(400, 1), (404, 1), (405, 1), (409, 1), (429, 3), (500, 3), (502, 3)],
)
def test_gerrit_rest_errors_keep_status_and_retry_only_transients(
    mocked_gerrit: responses.RequestsMock, status: int, attempts: int
) -> None:
    """Every HTTP error keeps its status; only 429 and 5xx are retried.

    python-gerrit-api raises its own exceptions for most statuses, which
    carry neither the status nor the response.
    """
    mocked_gerrit.add(responses.POST, _ANY_URL, status=status, body=b"no")

    client = GerritRestClient(
        base_url="https://gerrit.example.org/",
        auth=("user", "secret"),
        max_attempts=3,
    )
    with pytest.raises(GerritRestError) as ei:
        client.post("/changes/1/abandon", data={"message": "gone"})

    assert ei.value.status == status
    assert f"HTTP {status}" in str(ei.value)
    assert isinstance(ei.value.__cause__, requests.HTTPError)
    assert len(mocked_gerrit.calls) == attempts
    # A retried mutation sends the same request each time.
    assert {call.request.body for call in mocked_gerrit.calls} == {
        b'{"message": "gone"}'
    }


@pytest.mark.parametrize("method", ["post", "put"])
@pytest.mark.parametrize(
    ("data", "body"),
    [
        ({"message": "Abandoned"}, b'{"message": "Abandoned"}'),
        ({}, b"{}"),
        (None, None),
    ],
    ids=["object", "empty-object", "no-body"],
)
def test_gerrit_rest_sends_mutation_payloads_as_json(
    mocked_gerrit: responses.RequestsMock,
    method: str,
    data: dict[str, str] | None,
    body: bytes | None,
) -> None:
    """Payloads go as JSON, an empty object included.

    python-gerrit-api would send a dict as form data and drop an empty
    one.
    """
    mocked_gerrit.add(method.upper(), _ANY_URL, body=b")]}'\n{}")

    client = GerritRestClient(
        base_url="https://gerrit.example.org/", auth=("user", "secret")
    )
    getattr(client, method)("/changes/1/abandon", data=data)

    [call] = mocked_gerrit.calls
    assert call.request.body == body
    if body is not None:
        assert call.request.headers["Content-Type"].startswith(
            "application/json"
        )


def test_gerrit_rest_uses_the_configured_timeout(
    mocked_gerrit: responses.RequestsMock,
) -> None:
    """Every request carries the client's timeout.

    python-gerrit-api replaces a per-call timeout with its own, so the
    client sets it when it builds the library client.
    """
    mocked_gerrit.add(responses.GET, _ANY_URL, body=b"[]")
    mocked_gerrit.add(responses.PUT, _ANY_URL, body=b"{}")

    client = GerritRestClient(
        base_url="https://gerrit.example.org/", timeout=3.5
    )
    client.get("/changes/")
    client.put("/changes/1/message", data={"message": "m"})

    # responses records the send() keyword arguments on the request.
    assert [
        vars(c.request)["req_kwargs"]["timeout"] for c in mocked_gerrit.calls
    ] == [
        3.5,
        3.5,
    ]


def test_gerrit_rest_clients_do_not_share_sessions(
    mocked_gerrit: responses.RequestsMock,
) -> None:
    """Each client owns its session, so cookies and auth stay with it."""
    mocked_gerrit.add(
        responses.GET,
        _ANY_URL,
        body=b"[]",
        headers={"Set-Cookie": "GerritAccount=abc; Path=/"},
    )
    first = GerritRestClient(
        base_url="https://gerrit.example.org/", auth=("user", "secret")
    )
    second = GerritRestClient(base_url="https://gerrit.example.org/")

    first.get("/accounts/self")
    second.get("/changes/")
    first.close()

    assert first._session is not second._session
    assert "Cookie" not in mocked_gerrit.calls[1].request.headers
    assert "Authorization" not in mocked_gerrit.calls[1].request.headers


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

    def _run_cmd(
        cmd: list[str], cwd: Path | str | None = None, **_: Any
    ) -> Any:
        # The core passes cwd=self.workspace; construct hook path
        # deterministically
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

    # Mock curl_download to write a tiny payload (below 128 bytes) and missing
    # shebang
    def mock_curl_download(
        url: str, output_path: str, **kwargs: Any
    ) -> tuple[int, str]:
        Path(output_path).write_bytes(b"echo not a hook\n")
        return (0, "200")

    monkeypatch.setattr(
        "github2gerrit.external_api.curl_download",
        mock_curl_download,
        raising=True,
    )

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

    # Mock curl_download to write a realistic payload with shebang and
    # recognizable markers
    def mock_curl_download(
        url: str, output_path: str, **kwargs: Any
    ) -> tuple[int, str]:
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

    monkeypatch.setattr(
        "github2gerrit.external_api.curl_download",
        mock_curl_download,
        raising=True,
    )

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


def test_gerrit_rest_403_sets_auth_status_and_warns_once(
    mocked_gerrit: responses.RequestsMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 403 should produce an auth-tagged error and a single warning.

    Authentication/authorization failures are expected when credentials
    are missing or insufficient. They must be surfaced as a concise,
    default-visible warning (not an error-level traceback), and the
    resulting GerritRestError must carry the HTTP status so callers can
    distinguish auth problems from transient faults.
    """
    from github2gerrit.utils import reset_warning_once

    reset_warning_once()

    mocked_gerrit.add(responses.GET, _ANY_URL, status=403)

    client = GerritRestClient(
        base_url="https://gerrit.example.org/",
        timeout=0.1,
        max_attempts=3,
    )
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(GerritRestError) as ei,
    ):
        client.get("/changes/?q=owner:self&n=1")

    assert ei.value.status == 403
    assert ei.value.is_auth_error is True

    auth_warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "authentication failed" in rec.getMessage().lower()
    ]
    assert len(auth_warnings) == 1


def test_gerrit_rest_403_warning_is_deduplicated(
    mocked_gerrit: responses.RequestsMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated 403s within a run must warn at most once."""
    from github2gerrit.utils import reset_warning_once

    reset_warning_once()

    mocked_gerrit.add(responses.GET, _ANY_URL, status=403)

    client = GerritRestClient(
        base_url="https://gerrit.example.org/",
        timeout=0.1,
        max_attempts=1,
    )
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with pytest.raises(GerritRestError):
                client.get("/changes/?q=owner:self&n=1")

    auth_warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "authentication failed" in rec.getMessage().lower()
    ]
    assert len(auth_warnings) == 1


def test_warn_gerrit_credentials_unavailable_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The shared credentials-unavailable helper must warn once per run."""
    from github2gerrit.gerrit_rest import warn_gerrit_credentials_unavailable
    from github2gerrit.utils import reset_warning_once

    reset_warning_once()

    with caplog.at_level(logging.WARNING):
        warn_gerrit_credentials_unavailable()
        warn_gerrit_credentials_unavailable()
        warn_gerrit_credentials_unavailable()

    creds_warnings = [
        rec
        for rec in caplog.records
        if "credentials are not available" in rec.getMessage().lower()
    ]
    assert len(creds_warnings) == 1


@pytest.fixture
def sent(
    mocked_gerrit: responses.RequestsMock,
) -> list[requests.PreparedRequest]:
    """Answer every request with an empty JSON object and record it."""
    recorded: list[requests.PreparedRequest] = []

    def _answer(
        request: requests.PreparedRequest,
    ) -> tuple[int, dict[str, str], bytes]:
        recorded.append(request)
        return 200, {"Content-Type": "application/json"}, b")]}'\n{}"

    for method in (responses.GET, responses.POST, responses.PUT):
        mocked_gerrit.add_callback(method, _ANY_URL, callback=_answer)
    return recorded


@pytest.mark.parametrize("method", ["get", "post", "put"])
@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (
            "https://gerrit.example.org/",
            "https://gerrit.example.org/a/accounts/self",
        ),
        (
            "https://gerrit.example.org/r/",
            "https://gerrit.example.org/r/a/accounts/self",
        ),
    ],
)
def test_gerrit_rest_authenticated_requests_use_a_prefix(
    method: str,
    base_url: str,
    expected: str,
    sent: list[requests.PreparedRequest],
) -> None:
    """Gerrit authenticates REST calls only beneath ``/a/``.

    Without the prefix a server treats the call as anonymous, whatever
    ``Authorization`` header it carries, so every method has to use it.
    """
    client = GerritRestClient(base_url=base_url, auth=("user", "secret"))

    getattr(client, method)("/accounts/self")

    assert [req.url for req in sent] == [expected]
    assert sent[0].headers.get("Authorization", "").startswith("Basic ")


def test_gerrit_rest_a_prefix_is_not_doubled(
    sent: list[requests.PreparedRequest],
) -> None:
    """A base URL that already ends in ``/a/`` is used as given."""
    client = GerritRestClient(
        base_url="https://gerrit.example.org/a/", auth=("user", "secret")
    )

    client.get("/accounts/self")

    assert [req.url for req in sent] == [
        "https://gerrit.example.org/a/accounts/self"
    ]


def test_gerrit_rest_anonymous_requests_omit_a_prefix(
    sent: list[requests.PreparedRequest],
) -> None:
    """Without credentials the request goes to the plain REST path."""
    client = GerritRestClient(base_url="https://gerrit.example.org/")

    client.get("/accounts/self")

    assert [req.url for req in sent] == [
        "https://gerrit.example.org/accounts/self"
    ]
    assert sent[0].headers.get("Authorization") is None


class _RecordingServer:
    """Serve Gerrit-style JSON on loopback and record each request.

    A real socket puts the request line through http.client's own
    validation, which a stubbed opener never reaches.
    """

    def __init__(
        self, redirect_to: str | None = None, host: str = "127.0.0.1"
    ) -> None:
        self.requests: list[tuple[str, str | None]] = []
        self.proxy_auth: list[str | None] = []
        self.redirect_to = redirect_to
        server = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.requests.append(
                    (self.path, self.headers.get("Authorization"))
                )
                server.proxy_auth.append(
                    self.headers.get("Proxy-Authorization")
                )
                if (
                    server.redirect_to is not None
                    and self.path != urlsplit(server.redirect_to).path
                ):
                    self.send_response(302)
                    self.send_header("Location", server.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b")]}'\n[]"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

        server_class = http.server.ThreadingHTTPServer
        if ":" in host:

            class _V6Server(http.server.ThreadingHTTPServer):
                address_family = socket.AF_INET6

            server_class = _V6Server
        self._httpd = server_class((host, 0), _Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


@pytest.fixture
def recording_server() -> Iterator[Callable[..., _RecordingServer]]:
    """Start loopback servers on demand and stop them afterwards."""
    servers: list[_RecordingServer] = []

    def _start(
        redirect_to: str | None = None, host: str = "127.0.0.1"
    ) -> _RecordingServer:
        server = _RecordingServer(redirect_to, host)
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.close()


def test_gerrit_rest_encodes_spaces_in_query(
    recording_server: Callable[..., _RecordingServer],
) -> None:
    """A query with spaces reaches the server percent-encoded.

    Callers such as the closed-PR cleanup pass ``project:x status:open``
    verbatim, and http.client refuses a request line with a space in it.
    """
    server = recording_server()
    client = GerritRestClient(
        base_url=f"http://127.0.0.1:{server.port}/", max_attempts=1
    )

    assert client.get("/changes/?q=project:foo/bar status:open&n=100") == []

    [(path, _auth)] = server.requests
    assert " " not in path
    query = parse_qs(urlsplit(path).query)
    assert query["q"] == ["project:foo/bar status:open"]
    assert query["n"] == ["100"]


def test_gerrit_rest_keeps_existing_percent_escapes(
    recording_server: Callable[..., _RecordingServer],
) -> None:
    """An already-encoded query is sent as given, not encoded twice."""
    server = recording_server()
    client = GerritRestClient(
        base_url=f"http://127.0.0.1:{server.port}/", max_attempts=1
    )

    client.get("/changes/?q=project%3Afoo%20status%3Aopen&n=1")

    [(path, _auth)] = server.requests
    assert path == "/changes/?q=project%3Afoo%20status%3Aopen&n=1"


class _FailingServer:
    """Answer every request on loopback with an HTTP error.

    Connections stay open between requests, as HTTP/1.1 allows, so the
    count of connections shows whether the client reuses one or leaves
    it checked out. With ``truncate`` the server promises more body than
    it sends and then hangs up, so reading the body fails.
    """

    def __init__(self, status: int, body: bytes, truncate: bool) -> None:
        self.connections = 0
        self.requests = 0
        server = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                server.connections += 1

            def _answer(self) -> None:
                server.requests += 1
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                promised = len(body) + (64 if truncate else 0)
                self.send_header("Content-Length", str(promised))
                self.end_headers()
                self.wfile.write(body)
                if truncate:
                    self.close_connection = True

            do_GET = do_POST = do_PUT = _answer

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

        self._httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _Handler
        )
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


@pytest.fixture
def failing_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., _FailingServer]]:
    """Start a failing loopback server on demand, with retries unpaused."""
    monkeypatch.setattr("time.sleep", lambda s: None, raising=False)
    servers: list[_FailingServer] = []

    def _start(
        status: int, body: bytes = b"boom", truncate: bool = False
    ) -> _FailingServer:
        server = _FailingServer(status, body, truncate)
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.close()


@pytest.mark.parametrize(
    ("method", "status", "attempts"),
    [("get", 503, 3), ("post", 409, 1), ("put", 500, 3)],
)
def test_gerrit_rest_error_responses_release_their_connection(
    failing_server: Callable[..., _FailingServer],
    method: str,
    status: int,
    attempts: int,
) -> None:
    """An error response is read and its connection goes back to the pool.

    The session raises from a response hook, before Requests reads the
    body; a response left unread would keep its connection checked out,
    so every retry and later request would open another one.
    """
    server = failing_server(status)
    client = GerritRestClient(
        base_url=f"http://127.0.0.1:{server.port}/",
        auth=("user", "secret"),
        max_attempts=3,
    )
    send = getattr(client, method)
    args = () if method == "get" else ({"message": "m"},)

    for _ in range(2):
        with pytest.raises(GerritRestError) as ei:
            send("/changes/1/message", *args)
        assert ei.value.status == status
        cause = ei.value.__cause__
        assert isinstance(cause, requests.HTTPError)
        assert cause.response is not None
        assert cause.response.text == "boom"
    client.close()

    assert server.requests == 2 * attempts
    assert server.connections == 1


def test_gerrit_rest_error_keeps_status_when_its_body_cannot_be_read(
    failing_server: Callable[..., _FailingServer],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A body that fails to arrive does not hide the HTTP error.

    The error keeps its status and stays retryable, and the broken
    connection is closed rather than reused.
    """
    server = failing_server(502, truncate=True)
    client = GerritRestClient(
        base_url=f"http://127.0.0.1:{server.port}/", max_attempts=2
    )

    with (
        caplog.at_level(logging.DEBUG, logger="github2gerrit.gerrit_rest"),
        pytest.raises(GerritRestError) as ei,
    ):
        client.get("/changes/")

    assert "Could not read the body of HTTP 502" in caplog.text
    assert ei.value.status == 502
    assert "HTTP 502" in str(ei.value)
    assert isinstance(ei.value.__cause__, requests.HTTPError)
    assert server.requests == 2
    assert server.connections == 2


@pytest.mark.parametrize("target_host", ["127.0.0.1", "localhost"])
def test_gerrit_rest_drops_credentials_on_cross_origin_redirect(
    recording_server: Callable[..., _RecordingServer],
    target_host: str,
) -> None:
    """A redirect to another host or port must not carry the password."""
    target = recording_server()
    origin = recording_server(
        redirect_to=f"http://{target_host}:{target.port}/elsewhere"
    )
    client = GerritRestClient(
        base_url=f"http://127.0.0.1:{origin.port}/",
        auth=("user", "secret"),
        max_attempts=1,
    )

    client.get("/accounts/self")

    assert origin.requests[0][1] is not None
    assert target.requests == [("/elsewhere", None)]


def test_gerrit_rest_keeps_credentials_on_same_origin_redirect(
    recording_server: Callable[..., _RecordingServer],
) -> None:
    """A redirect within the origin still authenticates."""
    server = recording_server()
    server.redirect_to = f"http://127.0.0.1:{server.port}/a/moved"
    client = GerritRestClient(
        base_url=f"http://127.0.0.1:{server.port}/",
        auth=("user", "secret"),
        max_attempts=1,
    )

    assert client.get("/accounts/self") == []

    assert [path for path, _auth in server.requests] == [
        "/a/accounts/self",
        "/a/moved",
    ]
    assert all(auth is not None for _path, auth in server.requests)


def test_gerrit_rest_drops_proxy_credentials_on_redirect_past_proxy(
    recording_server: Callable[..., _RecordingServer],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """A redirect to a NO_PROXY host goes there directly, without secrets.

    Requests would keep the proxy the first request used; the session
    resolves it again for the new URL, so neither the proxy password
    nor the Gerrit password reaches the other host.
    """
    target = recording_server()
    proxy = recording_server(
        redirect_to=f"http://localhost:{target.port}/elsewhere"
    )
    clean_proxy_env.setenv(
        "HTTP_PROXY", f"http://puser:psecret@127.0.0.1:{proxy.port}"
    )
    clean_proxy_env.setenv("NO_PROXY", "localhost")
    client = GerritRestClient(
        base_url="http://gerrit.invalid/",
        auth=("user", "secret"),
        max_attempts=1,
    )

    client.get("/accounts/self")

    assert proxy.requests[0][0] == "http://gerrit.invalid/a/accounts/self"
    assert proxy.proxy_auth[0] == _basic("puser", "psecret")
    assert target.requests == [("/elsewhere", None)]
    assert target.proxy_auth == [None]


_PROXY_VARIABLES: tuple[str, ...] = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


@pytest.fixture
def clean_proxy_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Start with no proxy variables, whatever the runner exports."""
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    for name in _PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_gerrit_rest_routes_through_all_proxy(
    recording_server: Callable[..., _RecordingServer],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """ALL_PROXY alone carries the request, as it did in requests.

    urllib's ProxyHandler ignores the ``all`` entry for HTTP and HTTPS,
    so without the fallback the client would connect directly.
    """
    proxy = recording_server()
    clean_proxy_env.setenv("ALL_PROXY", f"http://127.0.0.1:{proxy.port}")
    client = GerritRestClient(base_url="http://gerrit.invalid/", max_attempts=1)

    assert client.get("/config/server/version") == []

    assert [path for path, _auth in proxy.requests] == [
        "http://gerrit.invalid/config/server/version"
    ]


def test_gerrit_rest_prefers_scheme_proxy_over_all_proxy(
    recording_server: Callable[..., _RecordingServer],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """A proxy set for the scheme wins over ALL_PROXY."""
    scheme_proxy = recording_server()
    all_proxy = recording_server()
    clean_proxy_env.setenv(
        "HTTP_PROXY", f"http://127.0.0.1:{scheme_proxy.port}"
    )
    clean_proxy_env.setenv("ALL_PROXY", f"http://127.0.0.1:{all_proxy.port}")
    client = GerritRestClient(base_url="http://gerrit.invalid/", max_attempts=1)

    client.get("/config/server/version")

    assert len(scheme_proxy.requests) == 1
    assert all_proxy.requests == []


def test_gerrit_rest_all_proxy_honours_no_proxy(
    recording_server: Callable[..., _RecordingServer],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """A NO_PROXY host is reached directly despite ALL_PROXY."""
    proxy = recording_server()
    target = recording_server()
    clean_proxy_env.setenv("ALL_PROXY", f"http://127.0.0.1:{proxy.port}")
    clean_proxy_env.setenv("NO_PROXY", "localhost")
    client = GerritRestClient(
        base_url=f"http://localhost:{target.port}/", max_attempts=1
    )

    client.get("/config/server/version")

    assert proxy.requests == []
    assert target.requests == [("/config/server/version", None)]


def test_gerrit_rest_no_proxy_network_bypasses_proxy(
    recording_server: Callable[..., _RecordingServer],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """An address inside a NO_PROXY network is reached directly.

    Requests matches IPv4 CIDR entries, which is how a runner reaches an
    internal subnet.
    """
    proxy = recording_server()
    target = recording_server()
    clean_proxy_env.setenv("HTTP_PROXY", f"http://localhost:{proxy.port}")
    clean_proxy_env.setenv("NO_PROXY", "example.org, 127.0.0.0/8")
    client = GerritRestClient(
        base_url=f"http://127.0.0.1:{target.port}/", max_attempts=1
    )

    client.get("/config/server/version")

    assert proxy.requests == []
    assert target.requests == [("/config/server/version", None)]


def _ipv6_loopback_available() -> bool:
    """Return True if a socket can listen on the IPv6 loopback address."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.skipif(
    not _ipv6_loopback_available(), reason="IPv6 loopback is not available"
)
def test_gerrit_rest_no_proxy_ipv6_network_bypasses_proxy(
    recording_server: Callable[..., _RecordingServer],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """An IPv6 address inside a NO_PROXY network is reached directly.

    Requests matches CIDR entries for IPv4 addresses alone; the session
    extends the match to IPv6.
    """
    target = recording_server(host="::1")
    proxy = recording_server()
    clean_proxy_env.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
    clean_proxy_env.setenv("NO_PROXY", "example.org,::/64")
    client = GerritRestClient(
        base_url=f"http://[::1]:{target.port}/", max_attempts=1
    )

    client.get("/config/server/version")

    assert proxy.requests == []
    assert target.requests == [("/config/server/version", None)]


def test_gerrit_rest_no_proxy_network_applies_after_redirect(
    recording_server: Callable[..., _RecordingServer],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """A redirect into a NO_PROXY network leaves the proxy behind."""
    target = recording_server()
    proxy = recording_server(
        redirect_to=f"http://127.0.0.1:{target.port}/elsewhere"
    )
    clean_proxy_env.setenv("HTTP_PROXY", f"http://localhost:{proxy.port}")
    clean_proxy_env.setenv("NO_PROXY", "127.0.0.0/8")
    client = GerritRestClient(base_url="http://gerrit.invalid/", max_attempts=1)

    assert client.get("/config/server/version") == []

    assert [path for path, _auth in proxy.requests] == [
        "http://gerrit.invalid/config/server/version"
    ]
    assert target.requests == [("/elsewhere", None)]


def test_gerrit_rest_no_proxy_network_excludes_other_addresses(
    recording_server: Callable[..., _RecordingServer],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """An address outside every NO_PROXY network still uses the proxy."""
    proxy = recording_server()
    clean_proxy_env.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
    clean_proxy_env.setenv("NO_PROXY", "10.0.0.0/8,::1/128")
    client = GerritRestClient(base_url="http://192.0.2.10/", max_attempts=1)

    client.get("/config/server/version")

    assert [path for path, _auth in proxy.requests] == [
        "http://192.0.2.10/config/server/version"
    ]


@pytest.mark.parametrize(
    ("host", "no_proxy", "bypass"),
    [
        ("10.23.4.5", "10.0.0.0/8", True),
        ("10.23.4.5", " localhost , 10.23.0.0/16 ", True),
        ("11.0.0.1", "10.0.0.0/8", False),
        ("fd00::1", "fd00::/8", True),
        ("fd00::1", "10.0.0.0/8", False),
        ("10.23.4.5", "10.0.0.0/33,not-a-network/8", False),
        ("gerrit.example.org", "10.0.0.0/8", False),
        ("10.23.4.5", "", False),
    ],
)
def test_in_no_proxy_network(host: str, no_proxy: str, bypass: bool) -> None:
    """CIDR entries match addresses inside them and nothing else."""
    from github2gerrit.gerrit_rest import _in_no_proxy_network

    assert _in_no_proxy_network(host, no_proxy) is bypass


class _RawListener:
    """Accept TCP connections on loopback and record what arrives."""

    def __init__(self) -> None:
        self.received = b""
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen()
        self._sock.settimeout(5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        with contextlib.suppress(OSError):
            conn, _addr = self._sock.accept()
            with conn:
                conn.settimeout(2)
                with contextlib.suppress(OSError):
                    while chunk := conn.recv(4096):
                        self.received += chunk

    def close(self) -> None:
        # Wake a pending accept() with an empty connection.
        with contextlib.suppress(OSError):
            socket.create_connection(
                ("127.0.0.1", self.port), timeout=2
            ).close()
        self._thread.join()
        self._sock.close()


@pytest.mark.parametrize("variable", ["HTTPS_PROXY", "ALL_PROXY"])
def test_gerrit_rest_https_proxy_starts_with_tls(
    clean_proxy_env: pytest.MonkeyPatch,
    variable: str,
) -> None:
    """An HTTPS request through a TLS proxy starts TLS before anything else.

    Neither CONNECT nor the proxy password may cross the wire in
    plaintext, as urllib would have sent them.
    """
    listener = _RawListener()
    try:
        clean_proxy_env.setenv(
            variable, f"https://puser:psecret@127.0.0.1:{listener.port}"
        )
        client = GerritRestClient(
            base_url="https://gerrit.invalid/",
            auth=("user", "secret"),
            timeout=1,
            max_attempts=1,
        )

        with pytest.raises(GerritRestError):
            client.get("/accounts/self")
    finally:
        listener.close()

    # A TLS handshake record, then the ClientHello it carries.
    assert listener.received[:1] == b"\x16"
    assert listener.received[5:6] == b"\x01"
    assert b"CONNECT" not in listener.received
    assert b"psecret" not in listener.received
    assert base64.b64encode(b"puser:psecret") not in listener.received


def test_gerrit_rest_does_not_connect_directly_without_socks_support(
    recording_server: Callable[..., _RecordingServer],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """A SOCKS proxy Requests cannot use fails rather than being skipped."""
    target = recording_server()
    clean_proxy_env.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    client = GerritRestClient(
        base_url=f"http://localhost:{target.port}/", max_attempts=1
    )

    with pytest.raises(GerritRestError, match="SOCKS"):
        client.get("/config/server/version")

    assert target.requests == []


def test_gerrit_rest_https_proxy_skipped_for_no_proxy_host(
    tls_server: tuple[_RecordingServer, _TlsAuthority, _TlsAuthority],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """An HTTPS host NO_PROXY covers is reached despite a TLS proxy."""
    server, authority, _other = tls_server
    clean_proxy_env.setenv("REQUESTS_CA_BUNDLE", str(authority.ca_file))
    clean_proxy_env.setenv("HTTPS_PROXY", "https://127.0.0.1:9")
    clean_proxy_env.setenv("NO_PROXY", "localhost")

    assert _tls_client(server).get("/accounts/self") == []

    assert [path for path, _auth in server.requests] == ["/a/accounts/self"]


@pytest.mark.filterwarnings("error::urllib3.exceptions.InsecureRequestWarning")
@pytest.mark.parametrize(
    "variables",
    [("HTTP_PROXY",), ("HTTP_PROXY", "HTTPS_PROXY"), ("ALL_PROXY",)],
)
def test_gerrit_rest_https_proxy_carries_http_requests(
    tls_server: tuple[_RecordingServer, _TlsAuthority, _TlsAuthority],
    clean_proxy_env: pytest.MonkeyPatch,
    variables: tuple[str, ...],
) -> None:
    """A TLS proxy still serves plain HTTP Gerrit, over TLS to the proxy.

    urllib hands the proxied request back to the opener as an HTTPS one,
    so a TLS proxy also set for HTTPS sees it a second time.
    """
    proxy, authority, _other = tls_server
    clean_proxy_env.setenv("REQUESTS_CA_BUNDLE", str(authority.ca_file))
    for variable in variables:
        clean_proxy_env.setenv(
            variable, f"https://puser:psecret@localhost:{proxy.port}"
        )
    client = GerritRestClient(base_url="http://gerrit.invalid/", max_attempts=1)

    assert client.get("/config/server/version") == []

    assert [path for path, _auth in proxy.requests] == [
        "http://gerrit.invalid/config/server/version"
    ]
    assert proxy.proxy_auth[0] is not None


def test_gerrit_rest_https_proxy_for_http_request_is_verified(
    tls_server: tuple[_RecordingServer, _TlsAuthority, _TlsAuthority],
    clean_proxy_env: pytest.MonkeyPatch,
) -> None:
    """A TLS proxy carrying plain HTTP must present a trusted certificate.

    Requests leaves TLS to such a proxy unverified, as it verifies only
    https URLs.
    """
    proxy, _authority, other = tls_server
    clean_proxy_env.setenv("REQUESTS_CA_BUNDLE", str(other.ca_file))
    clean_proxy_env.setenv(
        "HTTP_PROXY", f"https://puser:psecret@localhost:{proxy.port}"
    )
    client = GerritRestClient(base_url="http://gerrit.invalid/", max_attempts=1)

    with pytest.raises(GerritRestError, match="CERTIFICATE_VERIFY_FAILED"):
        client.get("/config/server/version")

    assert proxy.requests == []


@pytest.mark.parametrize(
    ("old_url", "new_url", "keeps_auth"),
    [
        ("https://g.example.org/a/x", "https://g.example.org/a/y", True),
        ("https://g.example.org/a/x", "https://g.example.org:443/a/y", True),
        ("http://g.example.org/a/x", "https://g.example.org/a/y", True),
        ("https://g.example.org/a/x", "http://g.example.org/a/y", False),
        ("https://g.example.org/a/x", "https://evil.example.org/a/y", False),
        ("https://g.example.org/a/x", "https://g.example.org:8443/a/y", False),
        ("http://g.example.org:8080/a/x", "https://g.example.org/a/y", False),
    ],
)
def test_redirect_keeps_credentials_only_where_safe(
    isolated_credentials: Path,
    old_url: str,
    new_url: str,
    keeps_auth: bool,
) -> None:
    """Only same-origin redirects and HTTP-to-HTTPS upgrades keep auth.

    A redirect never gains credentials from a netrc file either, even one
    with an entry for the new host.
    """
    from github2gerrit.gerrit_rest import _GerritSession

    netrc = isolated_credentials / "home" / ".netrc"
    netrc.write_text(
        "machine g.example.org login netrc-user password netrc-pw\n"
        "machine evil.example.org login netrc-user password netrc-pw\n",
        encoding="utf-8",
    )
    netrc.chmod(0o600)
    old = requests.Request("GET", old_url).prepare()
    response = requests.Response()
    response.request = old
    new = requests.Request(
        "GET",
        new_url,
        headers={"Authorization": _basic("user", "secret")},
    ).prepare()

    _GerritSession().rebuild_auth(new, response)

    assert new.headers.get("Authorization") == (
        _basic("user", "secret") if keeps_auth else None
    )


def test_anonymous_requests_never_read_netrc(
    isolated_credentials: Path,
    recording_server: Callable[..., _RecordingServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An anonymous client stays anonymous, through a redirect too.

    Requests would authenticate any request without credentials from a
    netrc entry for its host; only the credential resolver may decide.
    """
    target = recording_server()
    origin = recording_server(
        redirect_to=f"http://localhost:{target.port}/elsewhere"
    )
    netrc = isolated_credentials / "home" / ".netrc"
    netrc.write_text(
        "machine 127.0.0.1 login netrc-user password netrc-pw\n"
        "machine localhost login netrc-user password netrc-pw\n",
        encoding="utf-8",
    )
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    client = GerritRestClient(
        base_url=f"http://127.0.0.1:{origin.port}/", max_attempts=1
    )

    client.get("/config/server/version")

    assert origin.requests == [("/config/server/version", None)]
    assert target.requests == [("/elsewhere", None)]


class _TlsAuthority:
    """A private CA and a loopback server certificate it signed."""

    def __init__(self, directory: Path, name: str) -> None:
        now = datetime.datetime.now(datetime.UTC)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf_cert = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name(
                    [x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]
                )
            )
            .issuer_name(ca_name)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    ca_key.public_key()
                ),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        pem = serialization.Encoding.PEM
        self.ca_file = directory / f"{name}-ca.pem"
        self.ca_file.write_bytes(ca_cert.public_bytes(pem))
        self.cert_file = directory / f"{name}-server.pem"
        self.cert_file.write_bytes(leaf_cert.public_bytes(pem))
        self.key_file = directory / f"{name}-server.key"
        self.key_file.write_bytes(
            leaf_key.private_bytes(
                pem,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


@pytest.fixture
def tls_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[_RecordingServer, _TlsAuthority, _TlsAuthority]]:
    """Serve HTTPS on loopback with a certificate from a private CA.

    Also yields an unrelated CA that did not sign the server certificate.
    No trust override is set, so each test chooses its own.
    """
    for name in (
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    authority = _TlsAuthority(tmp_path, "gerrit")
    other = _TlsAuthority(tmp_path, "other")
    server = _RecordingServer()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(authority.cert_file, authority.key_file)
    server._httpd.socket = context.wrap_socket(
        server._httpd.socket, server_side=True
    )
    yield server, authority, other
    server.close()


def _tls_client(server: _RecordingServer) -> GerritRestClient:
    return GerritRestClient(
        base_url=f"https://localhost:{server.port}/",
        auth=("user", "secret"),
        max_attempts=1,
    )


@pytest.mark.parametrize("variable", ["REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
def test_gerrit_rest_trusts_ca_bundle_from_environment(
    tls_server: tuple[_RecordingServer, _TlsAuthority, _TlsAuthority],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    """A private CA named the way requests read it verifies the server.

    The variable is set after the client exists, so it is read when the
    request goes out rather than when the module or client is built.
    """
    server, authority, _other = tls_server
    client = _tls_client(server)
    monkeypatch.setenv(variable, str(authority.ca_file))

    assert client.get("/accounts/self") == []

    assert [path for path, _auth in server.requests] == ["/a/accounts/self"]


def test_gerrit_rest_ca_bundle_accepts_hashed_directory(
    tls_server: tuple[_RecordingServer, _TlsAuthority, _TlsAuthority],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A CA directory works as it did with requests."""
    server, authority, _other = tls_server
    ca_dir = tmp_path / "ca-dir"
    ca_dir.mkdir()
    cert = x509.load_pem_x509_certificate(authority.ca_file.read_bytes())
    openssl_hash = _openssl_subject_hash(authority.ca_file)
    if openssl_hash is None:
        pytest.skip("openssl is not available to hash the CA directory")
    (ca_dir / f"{openssl_hash}.0").write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
    )
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_dir))

    assert _tls_client(server).get("/accounts/self") == []


def _openssl_subject_hash(ca_file: Path) -> str | None:
    import shutil
    import subprocess

    openssl = shutil.which("openssl")
    if openssl is None:
        return None
    result = subprocess.run(
        [openssl, "x509", "-hash", "-noout", "-in", str(ca_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_gerrit_rest_requests_ca_bundle_wins_over_curl_ca_bundle(
    tls_server: tuple[_RecordingServer, _TlsAuthority, _TlsAuthority],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both set, REQUESTS_CA_BUNDLE decides, as in requests."""
    server, authority, other = tls_server
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(authority.ca_file))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(other.ca_file))

    assert _tls_client(server).get("/accounts/self") == []

    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(other.ca_file))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(authority.ca_file))

    with pytest.raises(GerritRestError, match="CERTIFICATE_VERIFY_FAILED"):
        _tls_client(server).get("/accounts/self")


@pytest.mark.parametrize("ca_bundle", [None, "other"])
def test_gerrit_rest_rejects_untrusted_certificate(
    tls_server: tuple[_RecordingServer, _TlsAuthority, _TlsAuthority],
    monkeypatch: pytest.MonkeyPatch,
    ca_bundle: str | None,
) -> None:
    """Verification stays on without a CA bundle that trusts the server."""
    server, _authority, other = tls_server
    if ca_bundle is not None:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(other.ca_file))

    with pytest.raises(GerritRestError, match="CERTIFICATE_VERIFY_FAILED"):
        _tls_client(server).get("/accounts/self")

    assert server.requests == []


def test_gerrit_rest_fails_on_missing_ca_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CA bundle path that does not exist fails rather than skipping TLS."""
    monkeypatch.setattr("time.sleep", lambda s: None, raising=False)
    missing = tmp_path / "missing.pem"
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(missing))
    client = GerritRestClient(
        base_url="https://gerrit.invalid/", max_attempts=1
    )

    with pytest.raises(GerritRestError, match="CA certificate bundle"):
        client.get("/accounts/self")


@pytest.fixture
def isolated_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Run with an empty home and working directory and no credentials."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    for name in (
        "NETRC",
        "GERRIT_HTTP_USER",
        "GERRIT_HTTP_PASSWORD",
        "GERRIT_SSH_USER_G2G",
        "GERRIT_HTTP_BASE_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _write_netrc(path: Path, login: str, password: str) -> Path:
    path.write_text(
        f"machine gerrit.example.org login {login} password {password}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _basic(login: str, password: str) -> str:
    token = base64.b64encode(f"{login}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


@pytest.mark.parametrize(
    ("base_path", "expected"),
    [
        ("", "https://gerrit.example.org/a/changes/"),
        ("r", "https://gerrit.example.org/r/a/changes/"),
    ],
)
def test_build_client_uses_netrc_named_by_environment(
    isolated_credentials: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    base_path: str,
    expected: str,
    sent: list[requests.PreparedRequest],
) -> None:
    """Credentials only NETRC names authenticate the request beneath /a/.

    pygerrit2 found them through requests when nothing else had; the
    resolver now does, so the client also reports them.
    """
    from github2gerrit.gerrit_rest import build_client_for_host

    netrc = _write_netrc(
        isolated_credentials / "custom-netrc", "test-bot", "synthetic-pw"
    )
    monkeypatch.setenv("NETRC", str(netrc))
    caplog.set_level(logging.DEBUG)

    client = build_client_for_host(
        "gerrit.example.org", base_path=base_path, max_attempts=1
    )
    client.get("/changes/?q=project:private status:open")

    assert client.is_authenticated
    [req] = sent
    assert req.url is not None
    assert req.url.startswith(expected)
    assert req.headers.get("Authorization") == _basic(
        "test-bot", "synthetic-pw"
    )
    token = _basic("test-bot", "synthetic-pw").split()[1]
    for record in caplog.records:
        assert "synthetic-pw" not in record.getMessage()
        assert token not in record.getMessage()


def test_build_client_ignores_netrc_env_when_netrc_disabled(
    isolated_credentials: Path,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[requests.PreparedRequest],
) -> None:
    """use_netrc=False keeps NETRC credentials out of the request."""
    from github2gerrit.gerrit_rest import build_client_for_host

    netrc = _write_netrc(
        isolated_credentials / "custom-netrc", "test-bot", "synthetic-pw"
    )
    monkeypatch.setenv("NETRC", str(netrc))

    client = build_client_for_host(
        "gerrit.example.org", base_path="", max_attempts=1, use_netrc=False
    )
    client.get("/changes/")

    assert not client.is_authenticated
    [req] = sent
    assert req.url == "https://gerrit.example.org/changes/"
    assert req.headers.get("Authorization") is None


@pytest.mark.parametrize(
    ("source", "expected_login"),
    [
        ("explicit", "cli-user"),
        ("explicit_netrc_file", "file-user"),
        ("home_netrc", "home-user"),
        ("environment", "env-user"),
        ("netrc_env_only", "netrc-env-user"),
    ],
)
def test_build_client_credential_precedence_over_netrc_env(
    isolated_credentials: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected_login: str,
    sent: list[requests.PreparedRequest],
) -> None:
    """NETRC is the last source tried, where pygerrit2 tried it.

    Explicit credentials, an explicit netrc file, a discovered .netrc
    and the GERRIT_HTTP_* variables each win over it.
    """
    from github2gerrit.gerrit_rest import build_client_for_host

    netrc = _write_netrc(
        isolated_credentials / "custom-netrc", "netrc-env-user", "pw-env"
    )
    monkeypatch.setenv("NETRC", str(netrc))
    kwargs: dict[str, Any] = {}
    if source == "explicit":
        kwargs = {"http_user": "cli-user", "http_password": "pw-cli"}
    elif source == "explicit_netrc_file":
        kwargs = {
            "netrc_file": _write_netrc(
                isolated_credentials / "explicit-netrc", "file-user", "pw-f"
            )
        }
    elif source == "home_netrc":
        _write_netrc(
            isolated_credentials / "home" / ".netrc", "home-user", "pw-home"
        )
    elif source == "environment":
        monkeypatch.setenv("GERRIT_HTTP_USER", "env-user")
        monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "pw-env-var")

    client = build_client_for_host(
        "gerrit.example.org", base_path="", max_attempts=1, **kwargs
    )
    client.get("/changes/")

    [req] = sent
    login = base64.b64decode(req.headers["Authorization"].split()[1]).decode()
    assert login.split(":", 1)[0] == expected_login


def test_build_client_explicit_netrc_file_without_host_skips_netrc_env(
    isolated_credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit netrc file pins the netrc lookup to that file."""
    from github2gerrit.gerrit_rest import build_client_for_host

    monkeypatch.setenv(
        "NETRC",
        str(
            _write_netrc(
                isolated_credentials / "custom-netrc", "netrc-env-user", "pw"
            )
        ),
    )
    explicit = isolated_credentials / "explicit-netrc"
    explicit.write_text("machine other.example.org login x password y\n")
    explicit.chmod(0o600)

    client = build_client_for_host(
        "gerrit.example.org", base_path="", netrc_file=explicit
    )

    assert not client.is_authenticated


def test_build_client_falls_back_to_home_netrc_past_local_file(
    isolated_credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ./.netrc without the host no longer hides ~/.netrc.

    requests read ~/.netrc for pygerrit2 whatever the working directory
    held, so credentials kept only there still authenticate.
    """
    from github2gerrit.gerrit_rest import build_client_for_host

    local = Path.cwd() / ".netrc"
    local.write_text("machine other.example.org login x password y\n")
    local.chmod(0o600)
    _write_netrc(isolated_credentials / "home" / ".netrc", "home-user", "pw")

    client = build_client_for_host("gerrit.example.org", base_path="")

    assert client.is_authenticated


def test_build_client_ignores_missing_netrc_env_file(
    isolated_credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NETRC naming no file leaves the client anonymous, not failing."""
    from github2gerrit.gerrit_rest import build_client_for_host

    monkeypatch.setenv("NETRC", str(isolated_credentials / "absent"))

    client = build_client_for_host("gerrit.example.org", base_path="")

    assert not client.is_authenticated


@pytest.mark.parametrize(
    ("entry", "login", "password"),
    [
        (
            "machine gerrit.example.org login test-bot "
            "password synthetic#suffix",
            "test-bot",
            "synthetic#suffix",
        ),
        (
            "machine gerrit.example.org account acct-bot password synthetic-pw",
            "acct-bot",
            "synthetic-pw",
        ),
    ],
    ids=["hash-in-password", "account-without-login"],
)
def test_build_client_parses_netrc_env_file_as_requests_did(
    isolated_credentials: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    login: str,
    password: str,
    sent: list[requests.PreparedRequest],
) -> None:
    """The NETRC fallback reads entries the way requests read them.

    requests used the standard library's parser, which keeps an
    unquoted ``#`` in a password and uses ``account`` when an entry
    has no ``login``.
    """
    from github2gerrit.gerrit_rest import build_client_for_host

    netrc = isolated_credentials / "custom-netrc"
    netrc.write_text(entry + "\n", encoding="utf-8")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))

    client = build_client_for_host(
        "gerrit.example.org", base_path="", max_attempts=1
    )
    client.get("/changes/")

    assert client.is_authenticated
    [req] = sent
    assert req.headers.get("Authorization") == _basic(login, password)


def test_build_client_finds_home_netrc_entry_discovery_missed(
    isolated_credentials: Path,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[requests.PreparedRequest],
) -> None:
    """An ~/.netrc entry only requests could read still authenticates.

    Discovery reads ~/.netrc first and misses an ``account`` entry; the
    fallback reads the same file again the way requests did.
    """
    from github2gerrit.gerrit_rest import build_client_for_host

    home_netrc = isolated_credentials / "home" / ".netrc"
    home_netrc.write_text(
        "machine gerrit.example.org account acct-bot password pw\n",
        encoding="utf-8",
    )
    home_netrc.chmod(0o600)

    client = build_client_for_host(
        "gerrit.example.org", base_path="", max_attempts=1
    )
    client.get("/changes/")

    [req] = sent
    assert req.headers.get("Authorization") == _basic("acct-bot", "pw")


def test_build_client_ignores_unparsable_netrc_env_file(
    isolated_credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NETRC file requests could not parse leaves the client anonymous."""
    from github2gerrit.gerrit_rest import build_client_for_host

    netrc = isolated_credentials / "custom-netrc"
    netrc.write_text(
        "machine gerrit.example.org login test-bot password pw\nbogus\n",
        encoding="utf-8",
    )
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))

    client = build_client_for_host("gerrit.example.org", base_path="")

    assert not client.is_authenticated


def _stub_gerrit_responses(
    mocked_gerrit: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, tuple[str, bytes]],
) -> list[str]:
    """Answer each path through the real client's parsing path."""
    seen: list[str] = []

    def _answer(
        request: requests.PreparedRequest,
    ) -> tuple[int, dict[str, str], bytes]:
        path = urlsplit(str(request.url)).path
        seen.append(path)
        if path not in pages:
            raise requests.ConnectionError("connection refused")
        content_type, body = pages[path]
        return 200, {"Content-Type": content_type}, body

    mocked_gerrit.add_callback(responses.GET, _ANY_URL, callback=_answer)
    monkeypatch.setattr(
        "github2gerrit.gerrit_urls._discover_base_path_for_host",
        lambda host: "",
    )
    return seen


def test_anonymous_preflight_succeeds_when_ui_returns_html(
    mocked_gerrit: responses.RequestsMock,
    isolated_credentials: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The anonymous probe asks a JSON endpoint, not the HTML dashboard.

    pygerrit2 accepted any body; the client parses JSON strictly, so a
    probe of /dashboard/self that returns the web UI would read as a
    failure although the server answered.
    """
    html = b"<!doctype html><html><body>Gerrit</body></html>"
    seen = _stub_gerrit_responses(
        mocked_gerrit,
        monkeypatch,
        {
            "/dashboard/self": ("text/html; charset=UTF-8", html),
            "/config/server/version": (
                "application/json",
                b')]}\'\n"3.9.1"',
            ),
        },
    )
    caplog.set_level(logging.WARNING)

    Orchestrator(workspace=isolated_credentials)._verify_gerrit_rest(
        "gerrit.example.org", ""
    )

    assert seen == ["/config/server/version"]
    assert not [
        r for r in caplog.records if "REST probe failed" in r.getMessage()
    ]


def test_anonymous_preflight_reports_network_failure(
    mocked_gerrit: responses.RequestsMock,
    isolated_credentials: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A server that cannot be reached still produces the warning."""
    _stub_gerrit_responses(mocked_gerrit, monkeypatch, {})
    caplog.set_level(logging.WARNING)

    Orchestrator(workspace=isolated_credentials)._verify_gerrit_rest(
        "gerrit.example.org", ""
    )

    assert [r for r in caplog.records if "REST probe failed" in r.getMessage()]
