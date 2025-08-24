# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
sitecustomize.py

This module is imported automatically by Python's site initialization when
present on sys.path. It is used here to make pytest/Coverage runs in CI more
robust by:

- Ensuring COVERAGE_FILE is set to a unique, per-process path in the system
  temporary directory when running under pytest, preventing accidental mixing
  of coverage data from different processes or previous runs.

- Proactively removing stale coverage data files (.coverage, .coverage.*)
  from common base directories (CWD, repository root, and GITHUB_WORKSPACE),
  which can otherwise cause "Can't combine statement coverage data with branch
  data" errors when pytest-cov attempts to combine results.

The logic is guarded so it only executes during pytest sessions and is
best-effort: failures to remove files are ignored.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path


def _is_pytest_session() -> bool:
    """Heuristically determine if we're running under pytest."""
    # PYTEST_CURRENT_TEST is set by pytest for each collected test.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    # xdist workers set this environment variable.
    if os.getenv("PYTEST_XDIST_WORKER"):
        return True
    # Many setups export PYTEST_ADDOPTS; not definitive but a useful hint.
    if os.getenv("PYTEST_ADDOPTS"):
        return True
    # GitHub Actions sets this when using the standard setup-python action
    # with pytest, but not reliable in isolation.
    return False


def _debug_enabled() -> bool:
    """Return True if verbose debug output is requested."""
    v = os.getenv("G2G_COV_DEBUG", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _dbg(msg: str) -> None:
    if _debug_enabled():
        try:
            print(f"[sitecustomize] {msg}", file=sys.stderr)
        except Exception:
            pass


def _remove_path(p: Path) -> None:
    try:
        if p.is_file() or p.is_symlink():
            p.unlink(missing_ok=True)
    except Exception as exc:
        _dbg(f"Failed to remove {p}: {exc}")


def _iter_cov_candidates(base: Path) -> Iterable[Path]:
    # Standard coverage file
    yield base / ".coverage"
    # Any parallel/leftover data artifacts
    try:
        for child in base.iterdir():
            name = child.name
            if name.startswith(".coverage.") and child.is_file():
                yield child
    except Exception as exc:
        _dbg(f"Failed to iterate {base}: {exc}")


def _clean_stale_coverage_files(
    bases: Iterable[Path], protect: Path | None
) -> None:
    protected = str(protect.resolve()) if protect else None
    for base in bases:
        try:
            base = base.resolve()
        except Exception:
            # Use as-is if resolution fails
            pass
        for candidate in _iter_cov_candidates(base):
            try:
                # Skip the protected target if present in candidates
                if protected and str(candidate.resolve()) == protected:
                    continue
            except Exception:
                # If resolve fails, fall through and try to remove
                pass
            _remove_path(candidate)


def _ensure_unique_coverage_file() -> Path:
    """Set COVERAGE_FILE to a unique temp path if not already set."""
    existing = os.getenv("COVERAGE_FILE", "").strip()
    if existing:
        try:
            return Path(existing)
        except Exception:
            # Fall through to create a sane default
            pass
    unique = (
        Path(tempfile.gettempdir())
        / f".coverage.pytest.{os.getpid()}.{uuid.uuid4().hex}"
    )
    os.environ["COVERAGE_FILE"] = str(unique)
    return unique


def _collect_base_dirs(repo_root: Path) -> set[Path]:
    bases: set[Path] = set()
    # Current working directory (pytest usually runs from the repo root)
    try:
        bases.add(Path.cwd())
    except Exception:
        pass
    # Repository root (directory containing this file)
    bases.add(repo_root)
    # GITHUB_WORKSPACE if defined
    gw = os.getenv("GITHUB_WORKSPACE", "").strip()
    if gw:
        try:
            bases.add(Path(gw))
        except Exception:
            pass
    return bases


def _main() -> None:
    if not _is_pytest_session():
        return

    repo_root = Path(__file__).resolve().parent
    cov_target = _ensure_unique_coverage_file()

    bases = _collect_base_dirs(repo_root)
    _dbg(f"Configured COVERAGE_FILE={cov_target}")
    _dbg(f"Cleaning coverage artifacts in: {', '.join(str(b) for b in bases)}")

    _clean_stale_coverage_files(bases, protect=cov_target)


# Execute on import (sitecustomize is imported automatically by Python's site)
try:
    _main()
except Exception as _exc:
    # Never break interpreter startup; debug info only if enabled.
    _dbg(f"Unexpected error during startup hook: {_exc}")
