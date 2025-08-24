# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Orchestrator subpackage.

Phase 1 extraction scaffold:
- Provides a namespace for incremental migration of logic from the legacy
  monolithic `core.Orchestrator` implementation toward a phased pipeline.
- Initial deliverable only extracts reconciliation invocation (see
  `reconciliation.py`) while preserving public behavior.

Subsequent phases will populate:
- `pipeline.py` for ordered phase coordination
- `context.py` for immutable run/repository context objects
- Additional modules (ssh, git_ops, mapping, backref, verification, etc.)

Import surface kept intentionally minimal to avoid premature coupling.
"""

from __future__ import annotations

from .reconciliation import perform_reconciliation  # re-export convenience


__all__ = ["perform_reconciliation"]
