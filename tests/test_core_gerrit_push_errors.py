# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for Gerrit push failure error analysis."""

from __future__ import annotations

import tempfile
from pathlib import Path

from github2gerrit.core import Orchestrator
from github2gerrit.gitutils import CommandError


class TestGerritPushErrorAnalysis:
    """Test the _analyze_gerrit_push_failure method for various SSH and Gerrit errors."""

    def setup_method(self) -> None:
        """Set up test orchestrator instance."""
        self.workspace = Path(tempfile.mkdtemp())
        self.orchestrator = Orchestrator(workspace=self.workspace)

    def test_ssh_host_key_verification_failed(self) -> None:
        """Test detection of SSH host key verification failures."""
        output = """
2025-08-28 10:09:52.126670 Running: git remote update gerrit
Fetching gerrit
No ED25519 host key is known for [gerrit.o-ran-sc.org]:29418 and you have requested strict checking.
Host key verification failed.
fatal: Could not read from remote repository.
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH host key verification failed" in result
        assert "GERRIT_KNOWN_HOSTS" in result
        assert "ssh-keyscan" in result

    def test_ssh_host_key_unknown_rsa(self) -> None:
        """Test detection of unknown RSA host key."""
        output = """
Fetching gerrit
No RSA host key is known for [gerrit.example.com]:29418 and you have requested strict checking.
Host key verification failed.
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH host key verification failed" in result
        assert "ssh-keyscan" in result

    def test_ssh_host_key_unknown_ecdsa(self) -> None:
        """Test detection of unknown ECDSA host key."""
        output = """
No ECDSA host key is known for [gerrit.example.com]:29418 and you have requested strict checking.
Host key verification failed.
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH host key verification failed" in result

    def test_ssh_authenticity_cannot_be_established(self) -> None:
        """Test detection of unknown host authenticity."""
        output = """
The authenticity of host '[gerrit.example.com]:29418' can't be established.
ED25519 key fingerprint is SHA256:abc123...
Are you sure you want to continue connecting (yes/no/[fingerprint])?
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH host key unknown" in result
        assert "GERRIT_KNOWN_HOSTS" in result

    def test_ssh_public_key_denied(self) -> None:
        """Test detection of SSH public key authentication failure."""
        output = """
git@gerrit.example.com: Permission denied (publickey).
fatal: Could not read from remote repository.
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH public key authentication failed" in result
        assert "SSH key may be invalid" in result

    def test_ssh_authentication_failed(self) -> None:
        """Test detection of general SSH authentication failure."""
        output = """
Authentication failed for user@gerrit.example.com
fatal: Could not read from remote repository.
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH authentication failed" in result

    def test_ssh_no_matching_host_key_type(self) -> None:
        """Test detection of unsupported SSH key algorithm."""
        output = """
Unable to negotiate with gerrit.example.com port 29418: no matching host key type found.
Their offer: ssh-rsa,ssh-dss
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH key type not supported" in result

    def test_ssh_invalid_key_format(self) -> None:
        """Test detection of invalid SSH key format."""
        output = """
key_load_public: invalid format
Load key "/path/to/key": invalid format
Permission denied (publickey).
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH key format is invalid" in result

    def test_ssh_permission_denied_general(self) -> None:
        """Test detection of general SSH permission denied."""
        output = """
Permission denied (publickey,gssapi-keyex,gssapi-with-mic).
fatal: Could not read from remote repository.
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH permission denied" in result

    def test_could_not_read_from_remote_repository(self) -> None:
        """Test detection of generic remote repository access failure."""
        output = """
fatal: Could not read from remote repository.

Please make sure you have the correct access rights
and the repository exists.
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "Could not read from remote repository" in result
        assert "SSH authentication" in result

    def test_connection_refused(self) -> None:
        """Test detection of connection refused errors."""
        output = """
ssh: connect to host gerrit.example.com port 29418: Connection refused
fatal: Could not read from remote repository.
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "Connection failed" in result
        assert "network connectivity" in result

    def test_connection_timeout(self) -> None:
        """Test detection of connection timeout errors."""
        output = """
ssh: connect to host gerrit.example.com port 29418: Connection timed out
fatal: Could not read from remote repository.
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "Connection failed" in result
        assert "network connectivity" in result

    def test_gerrit_missing_issue_id(self) -> None:
        """Test detection of missing Issue-ID in commit message."""
        output = """
remote: ERROR: [13916ae] missing Issue-Id in commit message footer
remote:
remote: Hint: to automatically insert Issue-Id, consider installing the hook:
remote:   gitdir=$(git rev-parse --git-dir); scp -p -P 29418 user@gerrit.example.com:hooks/commit-msg ${gitdir}/hooks/
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "Missing Issue-ID in commit message" in result

    def test_gerrit_commit_not_associated_to_issue(self) -> None:
        """Test detection of commit not associated to any issue."""
        output = """
remote: ERROR: commit 13916ae: commit not associated to any issue
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "Commit not associated to any issue" in result

    def test_gerrit_remote_rejected_with_reason(self) -> None:
        """Test detection of Gerrit remote rejection with specific reason."""
        output = """
To ssh://user@gerrit.example.com:29418/project/name
 ! [remote rejected] HEAD -> refs/for/master (prohibited by Gerrit: not permitted to create change)
error: failed to push some refs to 'ssh://user@gerrit.example.com:29418/project/name'
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "Gerrit rejected the push" in result
        assert "not permitted to create change" in result

    def test_gerrit_remote_rejected_no_reason(self) -> None:
        """Test detection of Gerrit remote rejection without specific reason."""
        output = """
To ssh://user@gerrit.example.com:29418/project/name
 ! [remote rejected] HEAD -> refs/for/master
error: failed to push some refs to 'ssh://user@gerrit.example.com:29418/project/name'
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "Gerrit rejected the push" in result

    def test_unknown_error_fallback(self) -> None:
        """Test fallback to unknown error for unrecognized failure patterns."""
        output = """
Some completely unknown error that doesn't match any pattern.
"""

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "Unknown error" in result

    def test_case_insensitive_matching(self) -> None:
        """Test that error detection is case-insensitive."""
        output = """
HOST KEY VERIFICATION FAILED.
Fatal: Could not read from remote repository.
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH host key verification failed" in result

    def test_stderr_and_stdout_combined(self) -> None:
        """Test that both stdout and stderr are analyzed for error patterns."""
        stdout = "Some regular output"
        stderr = "Host key verification failed."

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=stdout,
            stderr=stderr,
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "SSH host key verification failed" in result

    def test_multiple_error_patterns_first_match_wins(self) -> None:
        """Test that the first matching error pattern is returned."""
        output = """
Host key verification failed.
Permission denied (publickey).
        """

        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=output,
            stderr="",
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        # Should match the first pattern (host key verification) not permission denied
        assert "SSH host key verification failed" in result
        assert "public key authentication failed" not in result

    def test_traceback_suppression_in_non_verbose_mode(self) -> None:
        """Test that tracebacks are suppressed when verbose mode is disabled."""
        import io
        import logging
        import os

        from github2gerrit.utils import log_exception_conditionally

        # Ensure verbose mode is off
        os.environ.pop("G2G_VERBOSE", None)

        # Set up log capture
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logger = logging.getLogger("github2gerrit.test")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            # Create an exception scenario
            try:
                raise ValueError("Test exception for traceback suppression")  # noqa: TRY301
            except ValueError:
                log_exception_conditionally(logger, "Test error occurred")

            log_output = log_stream.getvalue()

            # Verify traceback is suppressed
            assert "Traceback" not in log_output
            assert "ValueError: Test exception" not in log_output
            assert "Test error occurred" in log_output

        finally:
            logger.removeHandler(handler)

    def test_traceback_shown_in_verbose_mode(self) -> None:
        """Test that tracebacks are shown when verbose mode is enabled."""
        import io
        import logging
        import os

        from github2gerrit.utils import log_exception_conditionally

        # Enable verbose mode
        os.environ["G2G_VERBOSE"] = "true"

        # Set up log capture
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logger = logging.getLogger("github2gerrit.test")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            # Create an exception scenario
            try:
                raise ValueError("Test exception for traceback display")  # noqa: TRY301
            except ValueError:
                log_exception_conditionally(logger, "Test error occurred")

            log_output = log_stream.getvalue()

            # Verify traceback is shown
            assert "Traceback" in log_output
            assert "ValueError: Test exception" in log_output
            assert "Test error occurred" in log_output

        finally:
            logger.removeHandler(handler)
            os.environ.pop("G2G_VERBOSE", None)
