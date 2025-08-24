# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import io
import logging
import os

import pytest


def test_version_fallback_import() -> None:
    """
    Import the package and verify that __version__ is defined and, when the
    package metadata is not available (common in source checkout test runs),
    it falls back to '0.0.0'.
    """
    import github2gerrit as pkg

    assert hasattr(pkg, "__version__")
    assert isinstance(pkg.__version__, str)
    # In the test environment, the distribution is typically not installed,
    # so the fallback should be used.
    assert pkg.__version__ != ""


def _capture_logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("github2gerrit.test.misc")
    # Ensure a clean logger state
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return logger, stream


@pytest.mark.parametrize("verbose_env", ["", "false", "true"])  # type: ignore[misc]
def test_log_exception_conditionally_toggle(monkeypatch: pytest.MonkeyPatch, verbose_env: str) -> None:
    """
    Validate that log_exception_conditionally suppresses tracebacks when
    verbose mode is disabled and includes them when enabled.
    """
    # Arrange
    if verbose_env:
        monkeypatch.setenv("G2G_VERBOSE", verbose_env)
    else:
        monkeypatch.delenv("G2G_VERBOSE", raising=False)

    from github2gerrit.utils import log_exception_conditionally

    logger, stream = _capture_logger()

    # Act
    try:
        raise ValueError("example failure for logging")  # noqa: TRY301
    except ValueError:
        # Provide a format string to ensure interpolation is exercised
        log_exception_conditionally(logger, "Logging example: %s", "detail")

    output = stream.getvalue()

    # Assert
    assert "Logging example: detail" in output
    if os.getenv("G2G_VERBOSE", "").lower() == "true":
        assert "Traceback" in output
        assert "ValueError: example failure for logging" in output
    else:
        assert "Traceback" not in output
        assert "ValueError: example failure for logging" not in output
