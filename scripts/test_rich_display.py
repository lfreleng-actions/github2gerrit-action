#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Test script to demonstrate Rich display functionality for github2gerrit.

This script demonstrates the Rich-formatted output that will be shown
when processing GitHub PRs with the enhanced CLI.
"""

import sys
import time
from pathlib import Path


# Add the src directory to the path so we can import our modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from github2gerrit.rich_display import RICH_AVAILABLE
from github2gerrit.rich_display import G2GProgressTracker
from github2gerrit.rich_display import console
from github2gerrit.rich_display import display_pr_info


def demo_pr_info_display():
    """Demonstrate PR information display."""
    console.print("\n🔍 Examining pull request in os-climate")

    # Sample PR information similar to what would be extracted
    pr_info = {
        "Repository": "os-climate/osc-github-devops",
        "PR Number": 1050,
        "Title": "Chore: Bump lfit/releng-reusable-workflows from 0.2.18 "
        "to 0.2.19",
        "Author": "dependabot[bot]",
        "State": "open",
        "Base Branch": "main",
        "Head Branch": "dependabot/github_actions/"
        "lfit/releng-reusable-workflows-0.2.19",
        "SHA": "a1b2c3d4...",
        "Files Changed": 4,
        "URL": "https://github.com/os-climate/osc-github-devops/pull/1050",
    }

    display_pr_info(pr_info, "Pull Request Details")


def demo_progress_tracking():
    """Demonstrate progress tracking functionality."""
    target = "os-climate/osc-github-devops/pull/1050"

    console.print(f"\n🔄 Starting GitHub to Gerrit processing for {target}")

    # Initialize progress tracker
    progress_tracker = G2GProgressTracker(target)
    progress_tracker.start()

    try:
        # Simulate various processing steps
        operations = [
            ("Getting source PR details...", 1.0),
            ("Validating configuration...", 0.5),
            ("Checking for duplicates...", 1.2),
            ("Preparing local checkout...", 2.0),
            ("Processing commits...", 1.8),
            ("Submitting to Gerrit...", 2.5),
            ("Updating PR status...", 0.8),
        ]

        for operation, duration in operations:
            progress_tracker.update_operation(operation)
            time.sleep(duration)

            # Simulate some events during processing
            if "checkout" in operation:
                progress_tracker.pr_processed()
            elif "Submitting" in operation:
                progress_tracker.change_submitted()
                console.print(
                    "🔗 Gerrit change URL: https://gerrit.example.com/c/project/+/12345"
                )

        progress_tracker.update_operation("Completed successfully")
        time.sleep(0.5)

    finally:
        progress_tracker.stop()

    # Show final summary
    summary = progress_tracker.get_summary()
    console.print("\n✅ Operation completed!")
    console.print(f"⏱️  Total time: {summary.get('elapsed_time', 'unknown')}")
    console.print(f"📊 PRs processed: {summary['prs_processed']}")
    console.print(f"🔄 Changes submitted: {summary['changes_submitted']}")


def demo_bulk_processing():
    """Demonstrate bulk processing progress."""
    target = "os-climate/osc-github-devops"

    console.print(f"\n🔍 Examining repository {target}")

    # Initialize progress tracker for bulk processing
    progress_tracker = G2GProgressTracker(target)
    progress_tracker.start()

    try:
        progress_tracker.update_operation("Getting repository and PRs...")
        time.sleep(1.0)

        # Simulate finding multiple PRs
        pr_count = 5
        progress_tracker.update_operation(f"Processing {pr_count} open PRs...")

        for i in range(1, pr_count + 1):
            progress_tracker.update_operation(f"Processing PR #{1000 + i}...")
            progress_tracker.pr_processed()
            time.sleep(0.8)

            # Simulate different outcomes
            if i <= 3:
                progress_tracker.change_submitted()
            elif i == 4:
                progress_tracker.duplicate_skipped()
            else:
                progress_tracker.add_error(f"PR #{1000 + i} processing failed")

        progress_tracker.update_operation("Bulk processing completed")
        time.sleep(0.5)

    finally:
        progress_tracker.stop()

    # Show final summary
    summary = progress_tracker.get_summary()
    console.print("\n⚠️  Bulk processing completed with some issues!")
    console.print(f"⏱️  Total time: {summary.get('elapsed_time', 'unknown')}")
    console.print(f"📊 PRs processed: {summary['prs_processed']}")
    console.print(f"✅ Changes submitted: {summary['changes_submitted']}")
    console.print(f"⏭️  Duplicates skipped: {summary['duplicates_skipped']}")
    console.print(f"❌ Errors: {summary['errors_count']}")


def main():
    """Run the Rich display demonstration."""
    console.print("🎨 Rich Display Demonstration for github2gerrit-action")
    console.print("=" * 60)

    if not RICH_AVAILABLE:
        console.print(
            "⚠️  Rich library not available - output will be plain text"
        )
    else:
        console.print("✅ Rich library available - formatted output enabled")

    # Demo 1: PR Info Display
    console.print("\n📋 Demo 1: Pull Request Information Display")
    console.print("-" * 45)
    demo_pr_info_display()

    # Demo 2: Single PR Progress Tracking
    console.print("\n📋 Demo 2: Single PR Progress Tracking")
    console.print("-" * 40)
    demo_progress_tracking()

    # Demo 3: Bulk Processing Progress
    console.print("\n📋 Demo 3: Bulk Processing Progress")
    console.print("-" * 35)
    demo_bulk_processing()

    console.print("\n🎉 Demonstration completed!")
    console.print("This shows the enhanced CLI output that users will see when")
    console.print("processing GitHub PRs with the github2gerrit-action tool.")


if __name__ == "__main__":
    main()
