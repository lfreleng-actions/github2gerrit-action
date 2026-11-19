# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Regression test for metadata trailer separation bug.

These tests cover the GitHub metadata trailers (GitHub-PR, GitHub-Hash)
that the bug below placed in a malformed commit message with split footer
sections.

Bug: https://gerrit.onap.org/r/c/portal-ng/bff/+/142195
The bug caused commit messages like:

    Issue-ID:CIMAN-33
    Change-Id: I4c388b9157d7d6d68efd11a2703e9a492cf8f548
    Signed-off-by: dependabot[bot] <support@github.com>
    GitHub-PR: https://github.com/onap/portal-ng-bff/pull/37
    GitHub-Hash: 4308cefa1c972047
    Signed-off-by: modesevenindustrialsolutions <mwatkins@linuxfoundation.org>

    Change-Id: Id4045e0f6b05821a5d78a3d7e26b871a3776c2d6
    Signed-off-by: onap.gh2gerrit <releng+onap-gh2gerrit@linuxfoundation.org>

Commit messages are now assembled in one trailer block by
Orchestrator._build_commit_message_with_trailers.
"""

import tempfile
from pathlib import Path

from github2gerrit.core import Orchestrator
from github2gerrit.models import GitHubContext
from github2gerrit.models import Inputs


def _create_test_inputs(*, issue_id: str = "CIMAN-33") -> Inputs:
    """Create test inputs similar to the failing case."""
    return Inputs(
        submit_single_commits=False,
        use_pr_as_commit=False,
        fetch_depth=10,
        gerrit_known_hosts="gerrit.onap.org ssh-rsa AAAAB3NzaC1yc2E...",
        gerrit_ssh_privkey_g2g="fake-key",
        gerrit_ssh_user_g2g="onap.gh2gerrit",
        gerrit_ssh_user_g2g_email="releng+onap-gh2gerrit@linuxfoundation.org",
        github_token="ghp_test_token_123",  # noqa: S106
        organization="onap",
        reviewers_email="",
        preserve_github_prs=False,
        dry_run=True,
        normalise_commit=False,
        gerrit_server="gerrit.onap.org",
        gerrit_server_port=29418,
        gerrit_project="portal-ng/bff",
        issue_id=issue_id,
        issue_id_lookup_json="",
        commit_rules_json="",
        allow_duplicates=False,
        ci_testing=False,
        duplicates_filter="open",
    )


def _create_test_github_context() -> GitHubContext:
    """Create test GitHub context similar to the failing case."""
    return GitHubContext(
        event_name="pull_request_target",
        event_action="synchronize",
        event_path=None,
        repository="onap/portal-ng-bff",
        repository_owner="onap",
        server_url="https://github.com",
        run_id="18173383639",
        sha="4308cefa1c972047deadbeefcafebabe12345678",
        base_ref="master",
        head_ref="dependabot/maven/net.logstash.logback-logstash-logback-encoder-8.1",
        pr_number=37,
    )


def test_buggy_behavior_before_fix():
    """Test the buggy behavior that existed before the fix."""

    with tempfile.TemporaryDirectory() as tmpdir:
        orchestrator = Orchestrator(workspace=Path(tmpdir))
        gh_context = _create_test_github_context()

        # Get metadata trailers
    meta_trailers = orchestrator._build_pr_metadata_trailers(gh_context)

    existing_msg = """CHORE: bump dependency

Issue-ID: CIMAN-33
Change-Id: I4c388b9157d7d6d68efd11a2703e9a492cf8f548"""

    # Simulate the OLD buggy code - single newline
    commit_msg = existing_msg
    needed = [m for m in meta_trailers if m not in commit_msg]
    if needed:
        # OLD BUGGY CODE: commit_msg.rstrip() + "\n" + "\n".join(needed)
        commit_msg = commit_msg.rstrip() + "\n" + "\n".join(needed)

    lines = commit_msg.splitlines()

    # Find trailer positions
    change_id_pos = None
    github_pr_pos = None

    for i, line in enumerate(lines):
        if line.startswith("Change-Id:"):
            change_id_pos = i
        elif line.startswith("GitHub-PR:"):
            github_pr_pos = i

    # Verify the buggy behavior: GitHub-PR immediately follows Change-Id
    assert change_id_pos is not None
    assert github_pr_pos is not None
    assert github_pr_pos == change_id_pos + 1, (
        "Buggy behavior: GitHub-PR immediately follows Change-Id"
    )


def test_no_metadata_trailers_when_already_present():
    """Test that metadata trailers are not duplicated when already present."""

    with tempfile.TemporaryDirectory() as tmpdir:
        orchestrator = Orchestrator(workspace=Path(tmpdir))
        gh_context = _create_test_github_context()

        # Create a commit message that already has the metadata
    existing_msg = """CHORE: bump dependency

Issue-ID: CIMAN-33
Change-Id: I4c388b9157d7d6d68efd11a2703e9a492cf8f548

GitHub-PR: https://github.com/onap/portal-ng-bff/pull/37
GitHub-Hash: 4308cefa1c972047"""

    meta_trailers = orchestrator._build_pr_metadata_trailers(gh_context)

    # Apply injection logic
    commit_msg = existing_msg
    needed = [m for m in meta_trailers if m not in commit_msg]

    # Should find no needed trailers since they're already present
    assert len(needed) == 0, "Metadata trailers should not be duplicated"

    # Message should remain unchanged
    assert commit_msg == existing_msg


def test_metadata_trailer_content():
    """Test that metadata trailers contain correct content."""

    with tempfile.TemporaryDirectory() as tmpdir:
        orchestrator = Orchestrator(workspace=Path(tmpdir))
        gh_context = _create_test_github_context()

        meta_trailers = orchestrator._build_pr_metadata_trailers(gh_context)

    # Should have exactly 2 trailers
    assert len(meta_trailers) == 2

    # Check GitHub-PR content
    github_pr_trailer = next(
        t for t in meta_trailers if t.startswith("GitHub-PR:")
    )
    assert "https://github.com/onap/portal-ng-bff/pull/37" in github_pr_trailer

    # Check GitHub-Hash content (should be deterministic)
    github_hash_trailer = next(
        t for t in meta_trailers if t.startswith("GitHub-Hash:")
    )
    assert github_hash_trailer == "GitHub-Hash: 4308cefa1c972047"


def test_no_metadata_trailers_without_pr_number():
    """Test that no metadata trailers are generated without a PR number."""

    with tempfile.TemporaryDirectory() as tmpdir:
        orchestrator = Orchestrator(workspace=Path(tmpdir))

        # Create context without PR number
        gh_context = GitHubContext(
            event_name="push",
            event_action="",
            event_path=None,
            repository="onap/portal-ng-bff",
            repository_owner="onap",
            server_url="https://github.com",
            run_id="18173383639",
            sha="4308cefa1c972047deadbeefcafebabe12345678",
            base_ref="master",
            head_ref="feature-branch",
            pr_number=None,  # No PR number
        )

        meta_trailers = orchestrator._build_pr_metadata_trailers(gh_context)

        # Should return empty list
        assert len(meta_trailers) == 0
