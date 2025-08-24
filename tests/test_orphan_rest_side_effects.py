# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for orphan REST side-effects implementation.

These tests specifically validate the actual Gerrit REST API calls made
during orphan policy enforcement (abandon/comment operations).
"""

from __future__ import annotations

import pytest

from github2gerrit.orchestrator.reconciliation import _abandon_orphan_changes
from github2gerrit.orchestrator.reconciliation import _comment_orphan_changes


class MockGerritRestClient:
    """Mock Gerrit REST client for testing."""

    def __init__(self):
        self.post_calls = []
        self.should_fail = {}

    def post(self, path: str, data: dict | None = None):
        """Record POST calls and optionally simulate failures."""
        self.post_calls.append((path, data))

        # Check if this specific path should fail
        if path in self.should_fail:
            from github2gerrit.gerrit_rest import GerritRestError

            raise GerritRestError(self.should_fail[path])

        return {"message": "success"}

    def make_fail(self, path: str, error_msg: str):
        """Configure a specific path to fail."""
        self.should_fail[path] = error_msg


class MockGerrit:
    """Mock Gerrit info object."""

    def __init__(self, host: str = "gerrit.example.org"):
        self.host = host


@pytest.fixture
def mock_client():
    """Provide a mock REST client."""
    return MockGerritRestClient()


@pytest.fixture
def mock_gerrit():
    """Provide a mock Gerrit info object."""
    return MockGerrit()


def test_abandon_orphan_changes_success(mock_client, mock_gerrit, monkeypatch):
    """Test successful abandon operations."""
    # Arrange
    orphan_ids = [
        "I1111111111111111111111111111111111111111",
        "I2222222222222222222222222222222222222222",
    ]

    def mock_build_client(host, **kwargs):
        assert host == mock_gerrit.host
        return mock_client

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", mock_build_client
    )

    # Act
    result = _abandon_orphan_changes(orphan_ids, mock_gerrit)

    # Assert
    assert result == orphan_ids  # All should succeed
    assert len(mock_client.post_calls) == 2

    # Check first abandon call
    path1, data1 = mock_client.post_calls[0]
    assert path1 == "/changes/I1111111111111111111111111111111111111111/abandon"
    assert data1 == {
        "message": "Abandoned due to GitHub PR update (orphaned change)"
    }

    # Check second abandon call
    path2, data2 = mock_client.post_calls[1]
    assert path2 == "/changes/I2222222222222222222222222222222222222222/abandon"
    assert data2 == {
        "message": "Abandoned due to GitHub PR update (orphaned change)"
    }


def test_abandon_orphan_changes_partial_failure(
    mock_client, mock_gerrit, monkeypatch, caplog
):
    """Test abandon operations with some failures."""
    # Arrange
    orphan_ids = [
        "I1111111111111111111111111111111111111111",
        "I2222222222222222222222222222222222222222",
    ]

    # Make the second abandon fail
    mock_client.make_fail(
        "/changes/I2222222222222222222222222222222222222222/abandon",
        "Change already abandoned",
    )

    def mock_build_client(host, **kwargs):
        return mock_client

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", mock_build_client
    )

    # Act
    result = _abandon_orphan_changes(orphan_ids, mock_gerrit)

    # Assert
    assert result == [
        "I1111111111111111111111111111111111111111"
    ]  # Only first succeeded
    assert len(mock_client.post_calls) == 2  # Both were attempted
    assert (
        "Failed to abandon change I2222222222222222222222222222222222222222"
        in caplog.text
    )


def test_comment_orphan_changes_success(mock_client, mock_gerrit, monkeypatch):
    """Test successful comment operations."""
    # Arrange
    orphan_ids = ["I3333333333333333333333333333333333333333"]

    def mock_build_client(host, **kwargs):
        assert host == mock_gerrit.host
        return mock_client

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", mock_build_client
    )

    # Act
    result = _comment_orphan_changes(orphan_ids, mock_gerrit)

    # Assert
    assert result == orphan_ids
    assert len(mock_client.post_calls) == 1

    path, data = mock_client.post_calls[0]
    assert (
        path
        == "/changes/I3333333333333333333333333333333333333333/revisions/current/review"
    )
    expected_message = (
        "This change has been orphaned by a GitHub PR update. It is no "
        "longer part of the current PR commit set."
    )
    assert data == {"message": expected_message}


def test_comment_orphan_changes_failure(
    mock_client, mock_gerrit, monkeypatch, caplog
):
    """Test comment operations with failures."""
    # Arrange
    orphan_ids = ["I4444444444444444444444444444444444444444"]

    # Make the comment fail
    mock_client.make_fail(
        "/changes/I4444444444444444444444444444444444444444/revisions/current/review",
        "Permission denied",
    )

    def mock_build_client(host, **kwargs):
        return mock_client

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", mock_build_client
    )

    # Act
    result = _comment_orphan_changes(orphan_ids, mock_gerrit)

    # Assert
    assert result == []  # No successes
    assert len(mock_client.post_calls) == 1  # Attempt was made
    assert (
        "Failed to comment on change I4444444444444444444444444444444444444444"
        in caplog.text
    )


def test_abandon_orphan_changes_no_ids():
    """Test abandon with empty orphan list."""
    result = _abandon_orphan_changes([], MockGerrit())
    assert result == []


def test_abandon_orphan_changes_no_gerrit():
    """Test abandon with None gerrit object."""
    result = _abandon_orphan_changes(
        ["I1111111111111111111111111111111111111111"], None
    )
    assert result == []


def test_comment_orphan_changes_no_ids():
    """Test comment with empty orphan list."""
    result = _comment_orphan_changes([], MockGerrit())
    assert result == []


def test_comment_orphan_changes_no_gerrit():
    """Test comment with None gerrit object."""
    result = _comment_orphan_changes(
        ["I1111111111111111111111111111111111111111"], None
    )
    assert result == []


def test_abandon_orphan_changes_client_creation_failure(
    mock_gerrit, monkeypatch, caplog
):
    """Test abandon when REST client creation fails."""
    # Arrange
    orphan_ids = ["I5555555555555555555555555555555555555555"]

    def mock_build_client_fail(host, **kwargs):
        raise ConnectionError("Network unreachable")

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host",
        mock_build_client_fail,
    )

    # Act
    result = _abandon_orphan_changes(orphan_ids, mock_gerrit)

    # Assert
    assert result == []
    assert (
        "Failed to create Gerrit REST client for abandon operations"
        in caplog.text
    )


def test_comment_orphan_changes_client_creation_failure(
    mock_gerrit, monkeypatch, caplog
):
    """Test comment when REST client creation fails."""
    # Arrange
    orphan_ids = ["I6666666666666666666666666666666666666666"]

    def mock_build_client_fail(host, **kwargs):
        raise ConnectionError("Network unreachable")

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host",
        mock_build_client_fail,
    )

    # Act
    result = _comment_orphan_changes(orphan_ids, mock_gerrit)

    # Assert
    assert result == []
    assert (
        "Failed to create Gerrit REST client for comment operations"
        in caplog.text
    )


def test_abandon_unexpected_error_handling(
    mock_client, mock_gerrit, monkeypatch, caplog
):
    """Test abandon with unexpected errors during operation."""
    # Arrange
    orphan_ids = ["I7777777777777777777777777777777777777777"]

    def mock_post_with_unexpected_error(path, data=None):
        raise ValueError("Unexpected error")  # Not a GerritRestError

    mock_client.post = mock_post_with_unexpected_error

    def mock_build_client(host, **kwargs):
        return mock_client

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", mock_build_client
    )

    # Act
    result = _abandon_orphan_changes(orphan_ids, mock_gerrit)

    # Assert
    assert result == []
    assert (
        "Unexpected error abandoning change I7777777777777777777777777777777777777777"
        in caplog.text
    )


def test_comment_unexpected_error_handling(
    mock_client, mock_gerrit, monkeypatch, caplog
):
    """Test comment with unexpected errors during operation."""
    # Arrange
    orphan_ids = ["I8888888888888888888888888888888888888888"]

    def mock_post_with_unexpected_error(path, data=None):
        raise ValueError("Unexpected error")  # Not a GerritRestError

    mock_client.post = mock_post_with_unexpected_error

    def mock_build_client(host, **kwargs):
        return mock_client

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", mock_build_client
    )

    # Act
    result = _comment_orphan_changes(orphan_ids, mock_gerrit)

    # Assert
    assert result == []
    assert (
        "Unexpected error commenting on change I8888888888888888888888888888888888888888"
        in caplog.text
    )
