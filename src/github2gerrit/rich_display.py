# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Rich display utilities for enhanced CLI output.

Provides formatted output for PR information, progress tracking, and
operation status using Rich formatting library.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from .rich_logging import RichDisplayContext
from .rich_logging import setup_rich_aware_logging


try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

    # Fallback classes for when Rich is not available
    class Live:  # type: ignore
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def update(self, *args: Any) -> None:
            pass

    class Text:  # type: ignore
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def append(self, *args: Any, **kwargs: Any) -> None:
            pass

    class Console:  # type: ignore
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def print(self, *args: Any, **kwargs: Any) -> None:
            print(*args)

    class Table:  # type: ignore
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def add_column(self, *args: Any, **kwargs: Any) -> None:
            pass

        def add_row(self, *args: Any, **kwargs: Any) -> None:
            pass


__all__ = [
    "RICH_AVAILABLE",
    "DummyProgressTracker",
    "G2GProgressTracker",
    "ProgressTracker",
    "console",
    "display_pr_info",
]

log = logging.getLogger("github2gerrit.rich_display")

# Global console instance
console = Console(markup=False) if RICH_AVAILABLE else Console()


def safe_console_print(
    message: str,
    *,
    style: str = "white",
    progress_tracker: Any = None,
    err: bool = False,
) -> None:
    """
    Safely print to console with proper progress tracker handling.

    This function ensures that progress displays are properly suspended
    and resumed around console output to prevent display corruption.

    Args:
        message: Message to print
        style: Rich style for the message
        progress_tracker: Optional progress tracker to suspend/resume
        err: Whether to print to stderr
    """
    # Use Rich display context to manage logging interference
    context_id = f"safe_console_print_{id(message)}"

    with RichDisplayContext(context_id):
        if progress_tracker:
            progress_tracker.suspend()

        try:
            if RICH_AVAILABLE and not err:
                console.print(message, style=style)
            else:
                print(
                    message, file=None if not err else __import__("sys").stderr
                )
        finally:
            if progress_tracker:
                progress_tracker.resume()


def safe_typer_echo(
    message: str,
    *,
    progress_tracker: Any = None,
    err: bool = False,
) -> None:
    """
    Safely use typer.echo with proper progress tracker handling.

    Args:
        message: Message to print
        progress_tracker: Optional progress tracker to suspend/resume
        err: Whether to print to stderr
    """
    # Use Rich display context to manage logging interference
    context_id = f"safe_typer_echo_{id(message)}"

    with RichDisplayContext(context_id):
        if progress_tracker:
            progress_tracker.suspend()

        try:
            import typer

            typer.echo(message, err=err)
        finally:
            if progress_tracker:
                progress_tracker.resume()


def display_pr_info(
    pr_info: dict[str, Any],
    title: str = "",
    progress_tracker: Any = None,
) -> None:
    """Display pull request information in a formatted table.

    Args:
        pr_info: Dictionary containing PR information
        title: Optional table title
        progress_tracker: Optional progress tracker to suspend/resume
    """
    # Use Rich display context to manage logging interference
    context_id = f"pr_info_display_{id(pr_info)}"

    if not RICH_AVAILABLE:
        # Fallback display for when Rich is not available
        with RichDisplayContext(context_id):
            if progress_tracker:
                progress_tracker.suspend()
            print(f"\n=== {title or 'Pull Request Information'} ===")
            for key, value in pr_info.items():
                print(f"{key:15}: {value}")
            print("=" * 50)
            if progress_tracker:
                progress_tracker.resume()
        return

    # Rich display with logging context
    with RichDisplayContext(context_id):
        table = Table(title=title)
        table.add_column("Property", style="cyan")
        table.add_column("Value", style="green")

        # Add rows for each piece of PR information
        for key, value in pr_info.items():
            table.add_row(str(key), str(value))

        if progress_tracker:
            progress_tracker.suspend()
        console.print(table)
        if progress_tracker:
            progress_tracker.resume()


class ProgressTracker:
    """Base progress tracker for GitHub and Gerrit operations."""

    def __init__(self, operation_type: str, target: str):
        """Initialize progress tracker for an operation.

        Args:
            operation_type: Type of operation (e.g., "GitHub to Gerrit")
            target: Target identifier (e.g., organization name, repository)
        """
        self.operation_type = operation_type
        self.target = target
        self.start_time = datetime.now(UTC)
        self.console = console

        # Progress counters
        self.current_operation = "Initializing..."
        self.errors_count = 0
        self.warnings_count = 0

        # Rich Live display
        self.live: Live | None = None
        self.rich_available = RICH_AVAILABLE
        self._rich_initially_available = RICH_AVAILABLE
        self.paused = False

        # Initialize Rich-aware logging on first progress tracker creation
        if RICH_AVAILABLE:
            setup_rich_aware_logging()

        # Fallback for when Rich is not available
        self._last_display = ""
        self._last_operation = ""

        # Rich display context for managing logging interference
        self._rich_context: RichDisplayContext | None = None

    def start(self) -> None:
        """Start the progress display with in-place updates."""
        # Start Rich display context to manage logging interference
        if self.rich_available:
            context_id = f"progress_tracker_{id(self)}"
            self._rich_context = RichDisplayContext(context_id)
            self._rich_context.__enter__()

            self.console.print(
                "🔄 GitHub to Gerrit ", style="bold blue", end=""
            )
            self.console.print(f"for {self.target}", style="bold cyan")
        else:
            print(f"🔄 GitHub to Gerrit for {self.target}")
        self._last_operation = ""

    def stop(self) -> None:
        """Stop the progress display."""
        # Clean up Rich display context
        if self._rich_context:
            import contextlib

            with contextlib.suppress(Exception):
                self._rich_context.__exit__(None, None, None)
            self._rich_context = None
        self.paused = False

    def suspend(self) -> None:
        """Temporarily pause the display to allow clean printing."""
        self.paused = True

    def resume(self) -> None:
        """Resume the display after it was suspended."""
        self.paused = False

    def update_operation(self, operation: str) -> None:
        """Update the current operation description."""
        self.current_operation = operation
        if not self.paused and operation != self._last_operation:
            if self.rich_available:
                # Just print the new operation - don't try in-place updates
                # with Rich
                self.console.print(f"📋 {operation}", style="dim white")
            else:
                print(f"📋 {operation}")
            self._last_operation = operation

    def add_error(self, message: str | None = None) -> None:
        """Increment the error counter."""
        self.errors_count += 1
        if message and not self.rich_available:
            # Only log when Rich is not available to avoid breaking clean
            # display
            log.error("Progress tracker error: %s", message)
        self._refresh_display()

    def add_warning(self, message: str | None = None) -> None:
        """Increment the warning counter."""
        self.warnings_count += 1
        if message and not self.rich_available:
            # Only log when Rich is not available to avoid breaking clean
            # display
            log.warning("Progress tracker warning: %s", message)
        self._refresh_display()

    def _refresh_display(self) -> None:
        """Refresh the display - no-op for simple print-based display."""

    def _generate_display_text(self) -> Text:
        """Generate the current progress display text."""
        if not self.rich_available:
            return Text()

        text = Text()

        # Main operation line
        text.append("🔄 ", style="bold blue")
        text.append(f"{self.operation_type}", style="bold cyan")
        text.append(f" for {self.target}", style="white")

        # Status counters
        if self.errors_count > 0 or self.warnings_count > 0:
            text.append(" | ", style="white")
            if self.errors_count > 0:
                text.append(f"{self.errors_count} errors", style="red")
            if self.warnings_count > 0:
                if self.errors_count > 0:
                    text.append(", ", style="white")
                text.append(f"{self.warnings_count} warnings", style="yellow")

        text.append("\n")

        # Current operation line
        text.append(f"📋 {self.current_operation}", style="dim white")

        # Elapsed time
        elapsed = datetime.now(UTC) - self.start_time
        text.append(
            f"\n⏱️  Elapsed: {self._format_duration(elapsed)}", style="dim blue"
        )

        return text

    def _fallback_display(self) -> None:
        """Fallback display method - disabled to prevent duplicates."""
        # Completely disable fallback display to prevent duplicate output

    def _format_duration(self, duration: timedelta) -> str:
        """Format a duration for display."""
        total_seconds = int(duration.total_seconds())
        minutes = total_seconds // 60
        seconds = total_seconds % 60

        if minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of the operation progress."""
        elapsed = datetime.now(UTC) - self.start_time

        return {
            "operation_type": self.operation_type,
            "target": self.target,
            "errors_count": self.errors_count,
            "warnings_count": self.warnings_count,
            "elapsed_time": self._format_duration(elapsed),
            "current_operation": self.current_operation,
        }


class G2GProgressTracker(ProgressTracker):
    """Specialized progress tracker for GitHub to Gerrit operations."""

    def __init__(self, target: str):
        super().__init__("GitHub to Gerrit", target)
        self.prs_processed = 0
        self.changes_submitted = 0
        self.changes_updated = 0
        self.duplicates_skipped = 0

    def pr_processed(self) -> None:
        """Mark that a PR was processed."""
        self.prs_processed += 1
        self._refresh_display()

    def change_submitted(self) -> None:
        """Mark that a new change was submitted to Gerrit."""
        self.changes_submitted += 1
        self._refresh_display()

    def change_updated(self) -> None:
        """Mark that an existing change was updated in Gerrit."""
        self.changes_updated += 1
        self._refresh_display()

    def duplicate_skipped(self) -> None:
        """Mark that a duplicate was skipped."""
        self.duplicates_skipped += 1
        self._refresh_display()

    def _generate_display_text(self) -> Text:
        """Generate G2G-specific display text."""
        if not self.rich_available:
            return Text()

        text = Text()

        # Main progress line
        text.append("🔄 GitHub to Gerrit ", style="bold blue")
        text.append(f"for {self.target}", style="bold cyan")

        # Stats
        if self.prs_processed > 0:
            text.append(f" | {self.prs_processed} PRs processed", style="white")

        if self.changes_submitted > 0:
            text.append(
                f" | {self.changes_submitted} new changes", style="green"
            )

        if self.changes_updated > 0:
            text.append(f" | {self.changes_updated} updated", style="yellow")

        if self.duplicates_skipped > 0:
            text.append(
                f" | {self.duplicates_skipped} duplicates skipped",
                style="dim white",
            )

        # Error/warning counts
        if self.errors_count > 0:
            text.append(f" | {self.errors_count} errors", style="red")
        if self.warnings_count > 0:
            text.append(f" | {self.warnings_count} warnings", style="yellow")

        text.append("\n")

        # Current operation line
        text.append(f"📋 {self.current_operation}", style="dim white")

        # Elapsed time
        elapsed = datetime.now(UTC) - self.start_time
        text.append(
            f"\n⏱️  Elapsed: {self._format_duration(elapsed)}", style="dim blue"
        )

        return text

    def get_summary(self) -> dict[str, Any]:
        """Get G2G-specific summary."""
        summary = super().get_summary()
        summary.update(
            {
                "prs_processed": self.prs_processed,
                "changes_submitted": self.changes_submitted,
                "changes_updated": self.changes_updated,
                "duplicates_skipped": self.duplicates_skipped,
            }
        )
        return summary


class DummyProgressTracker:
    """A no-op progress tracker for when progress display is disabled."""

    def __init__(self, operation_type: str, target: str):
        self.operation_type = operation_type
        self.target = target

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def suspend(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def update_operation(self, operation: str) -> None:
        pass

    def add_error(self, message: str | None = None) -> None:
        pass

    def add_warning(self, message: str | None = None) -> None:
        pass

    def pr_processed(self) -> None:
        pass

    def change_submitted(self) -> None:
        pass

    def change_updated(self) -> None:
        pass

    def duplicate_skipped(self) -> None:
        pass

    def get_summary(self) -> dict[str, Any]:
        return {"operation_type": self.operation_type, "target": self.target}
