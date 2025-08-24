#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Test script to simulate GitHub Actions environment and verify version logging.

This script tests that the github2gerrit CLI automatically logs version
information when running in a GitHub Actions environment.
"""

import os
import subprocess
import sys
from pathlib import Path


# Add the src directory to the path so we can import our modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_cli_mode():
    """Test CLI mode with --help (should show version)."""
    print("🧪 Testing CLI Mode with --help (should show version)")
    print("-" * 55)

    # Ensure we're NOT in GitHub Actions mode
    env = os.environ.copy()
    env.pop("GITHUB_ACTIONS", None)
    env.pop("GITHUB_EVENT_NAME", None)

    # Run the CLI help command
    result = subprocess.run(
        [sys.executable, "-m", "github2gerrit.cli", "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).parent.parent,
    )

    print(f"Exit code: {result.returncode}")
    print("STDOUT preview:")
    print(result.stdout[:200] + ("..." if len(result.stdout) > 200 else ""))

    # Check that version IS displayed (since --help was used)
    has_auto_version = "🏷️  github2gerrit version" in result.stdout
    print(f"Has version logging with --help: {has_auto_version}")

    if has_auto_version:
        print("✅ PASS: CLI mode correctly shows version with --help flag")
    else:
        print("❌ FAIL: CLI mode should show version when --help is used")

    print()
    return has_auto_version


def test_github_actions_mode():
    """Test GitHub Actions mode (should show automatic version logging)."""
    print(
        "🧪 Testing GitHub Actions Mode (should show automatic version logging)"
    )
    print("-" * 60)

    # Set GitHub Actions environment variables
    env = os.environ.copy()
    env["GITHUB_ACTIONS"] = "true"
    env["GITHUB_EVENT_NAME"] = "pull_request"
    env["GITHUB_REPOSITORY"] = "test/repo"

    # Run the CLI help command in GitHub Actions mode
    result = subprocess.run(
        [sys.executable, "-m", "github2gerrit.cli", "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).parent.parent,
    )

    print(f"Exit code: {result.returncode}")
    print("STDOUT preview:")
    print(result.stdout[:400] + ("..." if len(result.stdout) > 400 else ""))

    # Check that version IS automatically displayed
    has_auto_version = "🏷️  github2gerrit version" in result.stdout
    print(f"Has automatic version logging: {has_auto_version}")

    if has_auto_version:
        print(
            "✅ PASS: GitHub Actions mode correctly shows automatic "
            "version logging"
        )
    else:
        print(
            "❌ FAIL: GitHub Actions mode should show automatic version logging"
        )

    print()
    return has_auto_version


def test_cli_mode_without_help():
    """Test CLI mode without --help (should NOT show version)."""
    print("🧪 Testing CLI Mode without --help (should NOT show version)")
    print("-" * 58)

    # Test a command that will fail quickly without showing help
    result = subprocess.run(
        [sys.executable, "-m", "github2gerrit.cli", "--version"],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )

    print(f"Exit code: {result.returncode}")
    print("Version output:")
    print(result.stdout.strip())

    has_version_output = "github2gerrit version" in result.stdout
    has_emoji_version = "🏷️  github2gerrit version" in result.stdout

    if has_version_output and not has_emoji_version:
        print("✅ PASS: --version flag works without emoji (not help mode)")
    else:
        print("❌ FAIL: --version should show plain version, not emoji version")

    print()
    return has_version_output and not has_emoji_version


def test_explicit_version_flag():
    """Test explicit --version flag (should work in both modes)."""
    print("🧪 Testing Explicit --version Flag")
    print("-" * 35)

    # Test in normal mode
    result = subprocess.run(
        [sys.executable, "-m", "github2gerrit.cli", "--version"],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )

    print(f"Exit code: {result.returncode}")
    print("Version output:")
    print(result.stdout.strip())

    has_version_output = "github2gerrit version" in result.stdout
    if has_version_output:
        print("✅ PASS: --version flag works correctly")
    else:
        print("❌ FAIL: --version flag should show version information")

    print()
    return has_version_output


def test_version_in_github_actions_with_real_command():
    """Test version logging with a real command in GitHub Actions mode."""
    print("🧪 Testing Version Logging with Real Command in GitHub Actions")
    print("-" * 60)

    # Set GitHub Actions environment variables
    env = os.environ.copy()
    env["GITHUB_ACTIONS"] = "true"
    env["GITHUB_EVENT_NAME"] = "pull_request"
    env["GITHUB_REPOSITORY"] = "test/repo"

    # Run a command that would fail safely (no GitHub token)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "github2gerrit.cli",
            "https://github.com/test/repo/pull/123",
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).parent.parent,
    )

    print(f"Exit code: {result.returncode}")
    print("STDOUT (first 500 chars):")
    print(result.stdout[:500] + ("..." if len(result.stdout) > 500 else ""))

    if result.stderr:
        print("STDERR (first 200 chars):")
        print(result.stderr[:200] + ("..." if len(result.stderr) > 200 else ""))

    # Check that version IS displayed at the beginning
    has_auto_version = "🏷️  github2gerrit version" in result.stdout
    print(f"Has automatic version logging: {has_auto_version}")

    if has_auto_version:
        print("✅ PASS: Version logging appears in GitHub Actions mode")
    else:
        print("❌ FAIL: Version logging should appear in GitHub Actions mode")

    print()
    return has_auto_version


def main():
    """Run all tests and report results."""
    print("🎯 GitHub2Gerrit Version Logging Test Suite")
    print("=" * 50)
    print()

    # Run all tests
    test_results = []

    test_results.append(
        ("CLI Mode with --help (shows version)", test_cli_mode())
    )
    test_results.append(
        (
            "CLI Mode without --help (no emoji version)",
            test_cli_mode_without_help(),
        )
    )
    test_results.append(
        ("GitHub Actions Mode (auto version)", test_github_actions_mode())
    )
    test_results.append(
        ("Explicit --version flag", test_explicit_version_flag())
    )
    test_results.append(
        (
            "Real command in GitHub Actions",
            test_version_in_github_actions_with_real_command(),
        )
    )

    # Summary
    print("📊 Test Results Summary")
    print("=" * 25)

    passed = 0
    total = len(test_results)

    for test_name, result in test_results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} {test_name}")
        if result:
            passed += 1

    print()
    print(f"Overall: {passed}/{total} tests passed")

    if passed == total:
        print("🎉 All tests passed! Version logging works correctly.")
        return 0
    else:
        print("⚠️  Some tests failed. Check the implementation.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
