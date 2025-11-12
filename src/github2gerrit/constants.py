# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Shared constants for github2gerrit."""

# Gerrit change URL pattern
# Matches URLs with the structure: https://HOST[/SUBPATH]/c/PROJECT/+/NUMBER
# Examples:
#   https://gerrit.example.com/c/project/+/12345
#   https://gerrit.example.com/infra/c/releng/lftools/+/123
#   https://gerrit.example.com/c/nested/project/name/+/99999
#
# Pattern breakdown:
#   https?://           - Protocol (http or https)
#   ([^/]+)             - Capture group 1: hostname
#   /                   - Path separator
#   (?:[\w-]+/)*        - Optional subpath before /c/ (e.g., /infra/, /r/)
#                         More specific than .* to avoid greedy matching
#   c/                  - Gerrit change indicator
#   [^+]+               - Project name (can contain slashes for nested projects)
#   /\+/                - Gerrit's +/ separator
#   (\d+)               - Capture group 2: change number
GERRIT_CHANGE_URL_PATTERN = r"https?://([^/]+)/(?:[\w-]+/)*c/[^+]+/\+/(\d+)"

# GitHub PR URL pattern (supports both github.com and GitHub Enterprise)
# Matches URLs like:
#   https://github.com/owner/repo/pull/123
#   https://ghe.example.com/owner/repo/pull/123
GITHUB_PR_URL_PATTERN = r"https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)"
