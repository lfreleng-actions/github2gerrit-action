# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""
Plumbing shared by the fuzz harnesses.

The harness modules hold the contracts; this module holds what every one
of them needs around a contract: the exception an oracle raises, a
deterministic reader that turns fuzz bytes into structured values, and
the libFuzzer entry point that adds the seed directory.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class ContractViolation(AssertionError):
    """Raised when an input makes the code under test break its contract."""


def require(condition: bool, message: str) -> None:
    """Raise :class:`ContractViolation` with *message* unless *condition*.

    Harnesses call this instead of ``assert`` so that ``python -O`` keeps
    every check.
    """
    if not condition:
        raise ContractViolation(message)


def decode(data: bytes) -> str:
    """Decode fuzz bytes as UTF-8, replacing undecodable sequences."""
    return data.decode("utf-8", errors="replace")


class ByteReader:
    """Consume fuzz bytes as small integers, choices and strings.

    Reading past the end yields zeros, so every input, the empty one
    included, decodes to a value.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def byte(self) -> int:
        """Return the next byte, or 0 once the input is used up."""
        if self._pos >= len(self._data):
            return 0
        value = self._data[self._pos]
        self._pos += 1
        return value

    def flag(self) -> bool:
        """Return one bit of the next byte."""
        return bool(self.byte() & 1)

    def below(self, bound: int) -> int:
        """Return an integer in ``range(bound)`` from the next two bytes."""
        return ((self.byte() << 8) | self.byte()) % bound

    def word(self, alphabet: str, min_len: int, max_len: int) -> str:
        """Return a string of *alphabet* characters, its length in range."""
        length = min_len + self.byte() % (max_len - min_len + 1)
        return "".join(
            alphabet[self.byte() % len(alphabet)] for _ in range(length)
        )

    def text(self, max_len: int) -> str:
        """Return up to *max_len* bytes of the input, decoded."""
        length = self.byte() % (max_len + 1)
        chunk = self._data[self._pos : self._pos + length]
        self._pos += len(chunk)
        return decode(chunk)

    def rest(self) -> str:
        """Return all unread bytes, decoded."""
        chunk = self._data[self._pos :]
        self._pos = len(self._data)
        return decode(chunk)


def _names_input_file(argv: Sequence[str]) -> bool:
    return any(
        not arg.startswith("-") and Path(arg).is_file() for arg in argv[1:]
    )


def run(
    atheris: Any,
    check: Callable[[bytes], None],
    seeds: Sequence[bytes],
) -> None:
    """Fuzz *check* under Atheris, from *seeds* unless told otherwise.

    When an argument names an existing file, libFuzzer runs each named
    input once.  Otherwise the seeds are written to a temporary directory
    appended after any directories given, so a run with no arguments
    fuzzes from the seeds and a run given a corpus directory adds to it.
    libFuzzer exits the process when it finishes, so a run with no
    corpus directory leaves its seed directory in the temporary area.

    Each harness imports Atheris inside its own ``main`` and passes the
    module in: OpenSSF Scorecard and the organisation's fuzzing job find
    a harness by that import line, so it belongs in the harness files
    and in no other.  Importing it there, not at module level, lets the
    harnesses import, and the replay test run, without it installed.

    Args:
        atheris: The imported ``atheris`` module.
        check: The function to fuzz; it raises when an input breaks the
            contract it checks.
        seeds: Seed inputs.
    """
    atheris.instrument_all()
    # Log records cost time on every input and say nothing a crash does not.
    logging.disable(logging.CRITICAL)
    if _names_input_file(sys.argv):
        atheris.Setup(list(sys.argv), check)
        atheris.Fuzz()
        return
    with tempfile.TemporaryDirectory(prefix="g2g-fuzz-seeds-") as tmp:
        seed_dir = Path(tmp)
        for index, seed in enumerate(seeds):
            (seed_dir / f"seed-{index:03d}").write_bytes(seed)
        atheris.Setup([*sys.argv, str(seed_dir)], check)
        atheris.Fuzz()
