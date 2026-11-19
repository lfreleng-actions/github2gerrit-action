# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""
Fuzz harnesses for the parsers that read text this tool does not control.

Each ``fuzz_*.py`` module is a harness: run it with ``python`` to fuzz it
under Atheris, or import it to reach the plain check function it fuzzes.
``tests/test_fuzz_replay.py`` replays every seed through those checks.
"""
