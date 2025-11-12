# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import TypeVar


if TYPE_CHECKING:
    F = TypeVar("F", bound=Callable[..., object])

    def parametrize(*args: object, **kwargs: object) -> Callable[[F], F]: ...
else:
    from pytest import mark

    parametrize = mark.parametrize

from github2gerrit.cli import GitHubPRTarget
from github2gerrit.cli import GitHubRepoTarget
from github2gerrit.cli import _parse_github_target


@parametrize(
    "url, expected",
    [
        # Standard PR URLs
        (
            "https://github.com/onap/portal-ng-bff/pull/33",
            GitHubPRTarget(owner="onap", repo="portal-ng-bff", pr_number=33),
        ),
        (
            "https://www.github.com/onap/portal-ng-bff/pull/33",
            GitHubPRTarget(owner="onap", repo="portal-ng-bff", pr_number=33),
        ),
        # Repo URL (no PR number)
        (
            "https://github.com/onap/portal-ng-bff",
            GitHubRepoTarget(owner="onap", repo="portal-ng-bff"),
        ),
        # 'pulls' accepted as well
        (
            "https://github.com/onap/portal-ng-bff/pulls/33",
            GitHubPRTarget(owner="onap", repo="portal-ng-bff", pr_number=33),
        ),
        # Trailing slashes should be fine
        (
            "https://github.com/onap/portal-ng-bff/",
            GitHubRepoTarget(owner="onap", repo="portal-ng-bff"),
        ),
        # Query string and fragment should be ignored by parsing
        (
            "https://github.com/onap/portal-ng-bff/pull/33?foo=bar#section",
            GitHubPRTarget(owner="onap", repo="portal-ng-bff", pr_number=33),
        ),
        # Non-integer PR number: pr component should become None
        (
            "https://github.com/onap/portal-ng-bff/pull/not-a-number",
            GitHubRepoTarget(owner="onap", repo="portal-ng-bff"),
        ),
        # Non-GitHub domain: reject
        (
            "https://gitlab.com/onap/portal-ng-bff/pull/33",
            GitHubRepoTarget(owner=None, repo=None),
        ),
        # Insufficient path parts: reject
        ("https://github.com/onap", GitHubRepoTarget(owner=None, repo=None)),
        ("https://github.com/", GitHubRepoTarget(owner=None, repo=None)),
        ("https://github.com", GitHubRepoTarget(owner=None, repo=None)),
    ],
)
def test_parse_github_target(
    url: str, expected: GitHubPRTarget | GitHubRepoTarget
) -> None:
    assert _parse_github_target(url) == expected
