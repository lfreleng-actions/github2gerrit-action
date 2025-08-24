# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Rich-aware logging infrastructure to prevent interference with Rich displays.

This module provides a centralized logging system that detects when Rich
displays are active and routes log messages appropriately to avoid breaking
clean terminal output.

Key features:
- Automatic detection of Rich display contexts
- Clean routing of ERROR/WARNING logs through Rich console when available
- Background-only logging for DEBUG/INFO when Rich is active
- Seamless fallback to normal logging when Rich is not available
- Thread-safe operation for concurrent logging
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from typing import ClassVar


try:
    from rich.console import Console
    from rich.logging import RichHandler

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    Console = None
    RichHandler = None


class RichAwareLogger:
    """
    Rich-aware logging coordinator that manages logging behavior based on
    whether Rich displays are currently active.

    This class provides a global registry of active Rich display contexts
    and routes logging calls appropriately to prevent interference.
    """

    _instance: ClassVar[RichAwareLogger | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        """Initialize the Rich-aware logger."""
        self._active_rich_contexts: set[str] = set()
        self._rich_console: Console | None = None
        self._original_handlers: dict[str, list[logging.Handler]] = {}
        self._rich_handlers_installed = False
        self._context_lock = threading.Lock()

        if RICH_AVAILABLE:
            # Create a dedicated Rich console for logging
            self._rich_console = Console(stderr=True, markup=False)

    @classmethod
    def get_instance(cls) -> RichAwareLogger:
        """Get the singleton instance of RichAwareLogger."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def register_rich_context(self, context_id: str) -> None:
        """
        Register an active Rich display context.

        Args:
            context_id: Unique identifier for the Rich display context
        """
        with self._context_lock:
            was_empty = len(self._active_rich_contexts) == 0
            self._active_rich_contexts.add(context_id)

            # If this is the first Rich context, install Rich handlers
            if was_empty and RICH_AVAILABLE:
                self._install_rich_handlers()

    def unregister_rich_context(self, context_id: str) -> None:
        """
        Unregister a Rich display context.

        Args:
            context_id: Unique identifier for the Rich display context
        """
        with self._context_lock:
            self._active_rich_contexts.discard(context_id)

            # If no more Rich contexts, restore original handlers
            if len(self._active_rich_contexts) == 0:
                self._restore_original_handlers()

    def is_rich_active(self) -> bool:
        """Check if any Rich display contexts are currently active."""
        with self._context_lock:
            return len(self._active_rich_contexts) > 0 and RICH_AVAILABLE

    def _install_rich_handlers(self) -> None:
        """Install Rich-aware logging handlers."""
        if not RICH_AVAILABLE or self._rich_handlers_installed:
            return

        # Get the root logger and key module loggers
        loggers_to_modify = [
            logging.getLogger(),  # Root logger
            logging.getLogger("github2gerrit"),
            logging.getLogger("github2gerrit.cli"),
            logging.getLogger("github2gerrit.core"),
            logging.getLogger("github2gerrit.duplicate_detection"),
            logging.getLogger("github2gerrit.external_api"),
            logging.getLogger("github2gerrit.gerrit_rest"),
            logging.getLogger("github2gerrit.gitutils"),
        ]

        for logger in loggers_to_modify:
            logger_name = logger.name or "root"

            # Store original handlers
            self._original_handlers[logger_name] = logger.handlers.copy()

            # Remove existing handlers
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)

            # Add Rich-aware handler for ERROR and WARNING levels
            rich_handler = RichAwareHandler(self._rich_console)
            rich_handler.setLevel(
                logging.WARNING
            )  # Only handle WARNING and ERROR
            logger.addHandler(rich_handler)

            # Add silent handler for INFO and DEBUG (logs to file but not
            # console)
            silent_handler = SilentHandler()
            silent_handler.setLevel(logging.DEBUG)
            logger.addHandler(silent_handler)

        self._rich_handlers_installed = True

    def _restore_original_handlers(self) -> None:
        """Restore original logging handlers."""
        if not self._rich_handlers_installed:
            return

        for logger_name, original_handlers in self._original_handlers.items():
            logger = logging.getLogger(
                logger_name if logger_name != "root" else None
            )

            # Remove Rich handlers
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)

            # Restore original handlers
            for handler in original_handlers:
                logger.addHandler(handler)

        self._original_handlers.clear()
        self._rich_handlers_installed = False


class RichAwareHandler(logging.Handler):
    """
    Logging handler that routes ERROR/WARNING messages through Rich console
    when Rich displays are active.
    """

    def __init__(self, rich_console: Console | None = None) -> None:
        """Initialize the Rich-aware handler."""
        super().__init__()
        self._rich_console = rich_console

        # Set up formatting
        formatter = logging.Formatter("%(levelname)s: %(message)s")
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record through Rich console."""
        if not self._rich_console:
            return

        try:
            msg = self.format(record)

            # Choose style based on log level
            if record.levelno >= logging.ERROR:
                style = "red"
                prefix = "❌"
            elif record.levelno >= logging.WARNING:
                style = "yellow"
                prefix = "⚠️"
            else:
                style = "white"
                prefix = "i"

            # Print through Rich console
            self._rich_console.print(f"{prefix} {msg}", style=style)

        except Exception:
            # Fallback to handleError if Rich printing fails
            self.handleError(record)


class SilentHandler(logging.Handler):
    """
    Handler that silently discards log messages.

    This is used for INFO and DEBUG messages when Rich is active,
    to prevent them from interfering with Rich displays while still
    allowing them to be captured by file handlers if configured.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Silently discard the log record."""
        # Do nothing - this prevents console output


class RichDisplayContext:
    """
    Context manager for Rich display contexts.

    This should be used around any Rich display operations to ensure
    that logging doesn't interfere with clean Rich output.

    Example:
        with RichDisplayContext("progress_tracker"):
            # Rich display operations
            console.print(table)
            # Logging here won't interfere with Rich display
    """

    def __init__(self, context_id: str) -> None:
        """
        Initialize Rich display context.

        Args:
            context_id: Unique identifier for this display context
        """
        self.context_id = context_id
        self._logger = RichAwareLogger.get_instance()

    def __enter__(self) -> RichDisplayContext:
        """Enter the Rich display context."""
        self._logger.register_rich_context(self.context_id)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the Rich display context."""
        self._logger.unregister_rich_context(self.context_id)


def setup_rich_aware_logging() -> None:
    """
    Initialize Rich-aware logging for the entire application.

    This should be called early in the application lifecycle,
    typically in the main CLI entry point.
    """
    # Just ensure the singleton is created - actual setup happens
    # when Rich contexts are registered
    RichAwareLogger.get_instance()


def is_rich_logging_active() -> bool:
    """
    Check if Rich-aware logging is currently active.

    Returns:
        True if Rich displays are active and logging is being routed
        through Rich console, False otherwise.
    """
    return RichAwareLogger.get_instance().is_rich_active()


# Convenience functions for common logging patterns that are Rich-aware
def rich_error(message: str, *args: Any) -> None:
    """Log an error message in a Rich-aware manner."""
    logger = logging.getLogger("github2gerrit")
    logger.error(message, *args)


def rich_warning(message: str, *args: Any) -> None:
    """Log a warning message in a Rich-aware manner."""
    logger = logging.getLogger("github2gerrit")
    logger.warning(message, *args)


def rich_info(message: str, *args: Any) -> None:
    """Log an info message in a Rich-aware manner."""
    logger = logging.getLogger("github2gerrit")
    logger.info(message, *args)


def rich_debug(message: str, *args: Any) -> None:
    """Log a debug message in a Rich-aware manner."""
    logger = logging.getLogger("github2gerrit")
    logger.debug(message, *args)


__all__ = [
    "RichAwareLogger",
    "RichDisplayContext",
    "is_rich_logging_active",
    "rich_debug",
    "rich_error",
    "rich_info",
    "rich_warning",
    "setup_rich_aware_logging",
]
