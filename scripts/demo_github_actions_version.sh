#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

# Demo script showing GitHub Actions version logging behavior
# This script demonstrates how github2gerrit automatically displays version
# information when running in GitHub Actions environment vs CLI mode

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "🎭 GitHub2Gerrit Version Logging Demo"
echo "====================================="
echo

# Demo 1: CLI Mode (no automatic version logging)
echo "1️⃣  CLI Mode (Normal Usage)"
echo "   - No GITHUB_ACTIONS environment variable"
echo "   - Version only shown when explicitly requested"
echo

echo "Command: uv run python -m github2gerrit.cli --help"
echo "Output:"
echo "-------"
cd "$PROJECT_ROOT"
uv run python -m github2gerrit.cli --help | head -5
echo
echo "✅ Note: No automatic version display in CLI mode"
echo

# Demo 2: GitHub Actions Mode (automatic version logging)
echo "2️⃣  GitHub Actions Mode (CI/CD Usage)"
echo "   - GITHUB_ACTIONS=true environment variable set"
echo "   - Version automatically displayed at startup"
echo

echo "Command: GITHUB_ACTIONS=true uv run python -m github2gerrit.cli --help"
echo "Output:"
echo "-------"
GITHUB_ACTIONS=true uv run python -m github2gerrit.cli --help | head -6
echo
echo "✅ Note: Version automatically displayed with 🏷️ emoji in GitHub Actions mode"
echo

# Demo 3: Explicit version flag (works in both modes)
echo "3️⃣  Explicit Version Flag"
echo "   - Works the same in both CLI and GitHub Actions modes"
echo

echo "Command: uv run python -m github2gerrit.cli --version"
echo "Output:"
echo "-------"
uv run python -m github2gerrit.cli --version
echo
echo "✅ Note: Explicit --version flag works consistently in all modes"
echo

# Demo 4: Real command in GitHub Actions mode
echo "4️⃣  Real Command in GitHub Actions Mode"
echo "   - Shows version at startup, then processes command"
echo "   - Version appears in both console output and logs"
echo

echo "Command: GITHUB_ACTIONS=true uv run python -m github2gerrit.cli https://github.com/test/repo/pull/123 --dry-run"
echo "Output (first few lines):"
echo "-------------------------"
GITHUB_ACTIONS=true GITHUB_REPOSITORY=test/repo uv run python -m github2gerrit.cli https://github.com/test/repo/pull/123 --dry-run 2>&1 | head -8 || true
echo
echo "✅ Note: Version appears immediately, followed by normal processing"
echo

# Summary
echo "📋 Summary"
echo "=========="
echo "• CLI Mode: Clean output, version only on --version flag"
echo "• GitHub Actions Mode: Automatic version logging for CI/CD visibility"
echo "• Logging: Version info also written to structured logs in GitHub Actions"
echo "• Use Case: Helps verify exact version when run via uvx in CI/CD"
echo
echo "This ensures you can always confirm the exact version being executed"
echo "in your GitHub Actions workflows and CI/CD pipelines! 🚀"
