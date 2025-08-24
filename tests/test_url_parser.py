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

from github2gerrit.cli import _parse_github_target


@parametrize(
    "url, expected",
    [
        # Standard PR URLs
        (
            "https://github.com/onap/portal-ng-bff/pull/33",
            ("onap", "portal-ng-bff", 33),
        ),
        (
            "https://www.github.com/onap/portal-ng-bff/pull/33",
            ("onap", "portal-ng-bff", 33),
        ),
        # Repo URL (no PR number)
        (
            "https://github.com/onap/portal-ng-bff",
            ("onap", "portal-ng-bff", None),
        ),
        # 'pulls' accepted as well
        (
            "https://github.com/onap/portal-ng-bff/pulls/33",
            ("onap", "portal-ng-bff", 33),
        ),
        # Trailing slashes should be fine
        (
            "https://github.com/onap/portal-ng-bff/",
            ("onap", "portal-ng-bff", None),
        ),
        # Query string and fragment should be ignored by parsing
        (
            "https://github.com/onap/portal-ng-bff/pull/33?foo=bar#section",
            ("onap", "portal-ng-bff", 33),
        ),
        # Non-integer PR number: pr component should become None
        (
            "https://github.com/onap/portal-ng-bff/pull/not-a-number",
            ("onap", "portal-ng-bff", None),
        ),
        # Non-GitHub domain: reject
        (
            "https://gitlab.com/onap/portal-ng-bff/pull/33",
            (None, None, None),
        ),
        # Insufficient path parts: reject
        ("https://github.com/onap", (None, None, None)),
        ("https://github.com/", (None, None, None)),
        ("https://github.com", (None, None, None)),
    ],
)
def test_parse_github_target(
    url: str, expected: tuple[object, object, object]
) -> None:
    assert _parse_github_target(url) == expected
