# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Boolean options follow the documented configuration precedence.

A direct CLI run resolves each configurable boolean from, in order:
an explicit flag, the environment, the per-organisation configuration
file, then the CLI default. The CLI used to export every default before
the file was applied, and the file only fills variables still empty,
so it could set none of these options.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from github2gerrit import cli as cli_mod
from github2gerrit.cli import _CONFIGURABLE_BOOL_OPTIONS
from github2gerrit.cli import _bool_option
from github2gerrit.cli import _BoolOption
from github2gerrit.cli import _build_inputs_from_env
from github2gerrit.cli import app


OPTION_IDS = [opt.env_var for opt in _CONFIGURABLE_BOOL_OPTIONS]


def _str(value: bool) -> str:
    return "true" if value else "false"


def _cli_params() -> dict[str, Any]:
    # typer may vendor its own click, so match options by type name
    # rather than by isinstance against the installed click.
    command = typer.main.get_command(app)
    return {
        p.name: p
        for p in command.params
        if p.param_type_name == "option" and p.name
    }


def _flag_for(opt: _BoolOption) -> tuple[str, bool]:
    """Return a command-line flag for *opt* and the value it sets.

    Prefers the flag that states the default, so the file can carry the
    other value; an option with no negative form only offers ``True``.
    """
    param = _cli_params()[opt.param]
    if param.secondary_opts:
        flag = param.opts[0] if opt.default else param.secondary_opts[0]
        return flag, opt.default
    return param.opts[0], True


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for opt in _CONFIGURABLE_BOOL_OPTIONS:
        monkeypatch.delenv(opt.env_var, raising=False)
    monkeypatch.delenv("G2G_DISABLED", raising=False)
    monkeypatch.setenv("REVIEWERS_EMAIL", "reviewer@example.org")


def _run(
    argv: list[str],
    cfg: dict[str, str],
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Run the CLI up to its inputs and return the boolean variables.

    Processing is replaced by the step every entry point takes first,
    ``_load_effective_inputs``, with the configuration file supplied as
    *cfg*.
    """
    seen: dict[str, str] = {}

    def _process() -> None:
        cli_mod._load_effective_inputs()
        for opt in _CONFIGURABLE_BOOL_OPTIONS:
            seen[opt.env_var] = os.environ[opt.env_var]

    def _no_derivation(cfg: dict[str, str], *_: Any, **__: Any) -> Any:
        return cfg

    with (
        patch.object(cli_mod, "_run_processing", _process),
        patch.object(cli_mod, "load_org_config", return_value=dict(cfg)),
        patch.object(cli_mod, "apply_parameter_derivation", _no_derivation),
    ):
        result = CliRunner().invoke(app, argv, env=env)
    assert result.exit_code == 0, result.output
    return seen


@pytest.mark.parametrize("opt", _CONFIGURABLE_BOOL_OPTIONS, ids=OPTION_IDS)
class TestEachOption:
    def test_nothing_set_resolves_to_the_cli_default(
        self, opt: _BoolOption
    ) -> None:
        # Guards PRESERVE_GITHUB_PRS in particular: one of its readers
        # treats an unset variable as false and would close the pull
        # request.
        seen = _run([], {})
        assert seen[opt.env_var] == _str(opt.default)

    def test_the_file_alone_sets_the_value(self, opt: _BoolOption) -> None:
        seen = _run([], {opt.env_var: _str(not opt.default)})
        assert seen[opt.env_var] == _str(not opt.default)

    def test_a_flag_outranks_the_file(self, opt: _BoolOption) -> None:
        flag, value = _flag_for(opt)
        seen = _run([flag], {opt.env_var: _str(not value)})
        assert seen[opt.env_var] == _str(value)

    def test_the_environment_outranks_the_file(self, opt: _BoolOption) -> None:
        seen = _run(
            [],
            {opt.env_var: _str(not opt.default)},
            env={opt.env_var: _str(opt.default)},
        )
        assert seen[opt.env_var] == _str(opt.default)

    def test_a_file_value_is_normalised(self, opt: _BoolOption) -> None:
        # Not every reader parses the variable the same way.
        seen = _run([], {opt.env_var: "yes" if not opt.default else "no"})
        assert seen[opt.env_var] == _str(not opt.default)

    def test_the_cli_and_reader_defaults_agree(self, opt: _BoolOption) -> None:
        assert _cli_params()[opt.param].default is opt.default
        assert _bool_option(opt.env_var) is opt.default


def test_inputs_read_the_cli_defaults() -> None:
    inputs = _build_inputs_from_env()
    for opt in _CONFIGURABLE_BOOL_OPTIONS:
        if hasattr(inputs, opt.param):
            assert getattr(inputs, opt.param) is opt.default, opt.param


def test_every_configurable_option_is_a_known_key() -> None:
    from github2gerrit.config import KNOWN_KEYS

    assert {opt.env_var for opt in _CONFIGURABLE_BOOL_OPTIONS} <= KNOWN_KEYS
