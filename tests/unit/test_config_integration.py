# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Tests for configuration integration with SSH config parsing.

This module tests the integration of SSH config parsing into the main
configuration system, ensuring that credential derivation works correctly
with the existing configuration loading and derivation mechanisms.
"""

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from github2gerrit.config import apply_parameter_derivation
from github2gerrit.config import derive_gerrit_parameters
from github2gerrit.ssh_config_parser import clear_credential_cache


class TestDeriveGerritParameters:
    """Test cases for derive_gerrit_parameters function."""

    def setup_method(self):
        """Clear caches before each test to ensure isolation."""
        clear_credential_cache()

    def test_no_organization(self):
        """Test behavior when no organization is provided."""
        result = derive_gerrit_parameters(None)
        assert result == {}

    def test_empty_organization(self):
        """Test behavior with empty organization string."""
        result = derive_gerrit_parameters("")
        assert result == {}

    @mock.patch("github2gerrit.config.load_org_config")
    @mock.patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    def test_ssh_config_integration_success(
        self, mock_derive, mock_load_config
    ):
        """Test successful integration with SSH config parsing."""
        # Mock configuration loading
        mock_load_config.return_value = {
            "GERRIT_SERVER": "gerrit.linuxfoundation.org"
        }

        # Mock credential derivation
        mock_derive.return_value = ("sshuser", "test@example.com")

        result = derive_gerrit_parameters("lfit")

        expected = {
            "GERRIT_SSH_USER_G2G": "sshuser",
            "GERRIT_SSH_USER_G2G_EMAIL": "test@example.com",
            "GERRIT_SERVER": "gerrit.linuxfoundation.org",
        }
        assert result == expected

        mock_load_config.assert_called_once_with("lfit")
        mock_derive.assert_called_once_with(
            "gerrit.linuxfoundation.org", "lfit"
        )

    @mock.patch("github2gerrit.config.load_org_config")
    @mock.patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    def test_ssh_config_partial_fallback(self, mock_derive, mock_load_config):
        """Test partial fallback when SSH config provides only some credentials."""
        mock_load_config.return_value = {}
        mock_derive.return_value = ("sshuser", None)  # No git email found

        result = derive_gerrit_parameters("testorg")

        expected = {
            "GERRIT_SSH_USER_G2G": "sshuser",
            "GERRIT_SSH_USER_G2G_EMAIL": "releng+testorg-gh2gerrit@linuxfoundation.org",
            "GERRIT_SERVER": "gerrit.testorg.org",
        }
        assert result == expected

    @mock.patch("github2gerrit.config.load_org_config")
    @mock.patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    def test_ssh_config_full_fallback(self, mock_derive, mock_load_config):
        """Test full fallback when SSH config provides no credentials."""
        mock_load_config.return_value = {}
        mock_derive.return_value = (None, None)

        result = derive_gerrit_parameters("testorg")

        expected = {
            "GERRIT_SSH_USER_G2G": "testorg.gh2gerrit",
            "GERRIT_SSH_USER_G2G_EMAIL": "releng+testorg-gh2gerrit@linuxfoundation.org",
            "GERRIT_SERVER": "gerrit.testorg.org",
        }
        assert result == expected

    @mock.patch("github2gerrit.config.load_org_config")
    def test_ssh_config_import_error_fallback(self, mock_load_config):
        """Test fallback behavior when SSH config parser import fails."""
        mock_load_config.return_value = {}

        with mock.patch(
            "github2gerrit.ssh_config_parser.derive_gerrit_credentials"
        ) as mock_derive:
            mock_derive.side_effect = ImportError("Module not found")

            result = derive_gerrit_parameters("testorg")

            expected = {
                "GERRIT_SSH_USER_G2G": "testorg.gh2gerrit",
                "GERRIT_SSH_USER_G2G_EMAIL": "releng+testorg-gh2gerrit@linuxfoundation.org",
                "GERRIT_SERVER": "gerrit.testorg.org",
            }
            assert result == expected

    @mock.patch("github2gerrit.config.load_org_config")
    @mock.patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    def test_configured_server_overrides_default(
        self, mock_derive, mock_load_config
    ):
        """Test that configured server overrides default organization pattern."""
        mock_load_config.return_value = {
            "GERRIT_SERVER": "custom.gerrit.server.com"
        }
        mock_derive.return_value = ("customuser", "custom@example.com")

        result = derive_gerrit_parameters("testorg")

        expected = {
            "GERRIT_SSH_USER_G2G": "customuser",
            "GERRIT_SSH_USER_G2G_EMAIL": "custom@example.com",
            "GERRIT_SERVER": "custom.gerrit.server.com",
        }
        assert result == expected

        mock_derive.assert_called_once_with(
            "custom.gerrit.server.com", "testorg"
        )

    def test_case_normalization(self):
        """Test that organization name is normalized to lowercase."""
        with (
            mock.patch(
                "github2gerrit.config.load_org_config"
            ) as mock_load_config,
            mock.patch(
                "github2gerrit.ssh_config_parser.derive_gerrit_credentials"
            ) as mock_derive,
        ):
            mock_load_config.return_value = {}
            mock_derive.return_value = (None, None)

            derive_gerrit_parameters("TestOrg")

            mock_load_config.assert_called_once_with("testorg")
            mock_derive.assert_called_once_with("gerrit.testorg.org", "testorg")


class TestApplyParameterDerivation:
    """Test cases for apply_parameter_derivation function."""

    def setup_method(self):
        """Clear caches before each test to ensure isolation."""
        clear_credential_cache()

    def test_no_organization(self):
        """Test that no derivation occurs without organization."""
        cfg = {"EXISTING_KEY": "existing_value"}

        result = apply_parameter_derivation(cfg, organization=None)

        assert result == cfg

    @mock.patch("github2gerrit.config.derive_gerrit_parameters")
    @mock.patch("github2gerrit.config._is_github_actions_context")
    def test_derivation_disabled(self, mock_is_actions, mock_derive):
        """Test that derivation can be disabled via environment variable."""
        mock_is_actions.return_value = False

        with mock.patch.dict(os.environ, {"G2G_ENABLE_DERIVATION": "false"}):
            cfg = {}
            result = apply_parameter_derivation(cfg, "testorg")

            assert result == cfg
            mock_derive.assert_not_called()

    @mock.patch("github2gerrit.config.derive_gerrit_parameters")
    @mock.patch("github2gerrit.config._is_github_actions_context")
    def test_derivation_enabled_by_default(self, mock_is_actions, mock_derive):
        """Test that derivation is enabled by default."""
        mock_is_actions.return_value = False
        mock_derive.return_value = {
            "GERRIT_SSH_USER_G2G": "deriveduser",
            "GERRIT_SSH_USER_G2G_EMAIL": "derived@example.com",
        }

        cfg = {}
        result = apply_parameter_derivation(
            cfg, "testorg", save_to_config=False
        )

        expected = {
            "GERRIT_SSH_USER_G2G": "deriveduser",
            "GERRIT_SSH_USER_G2G_EMAIL": "derived@example.com",
        }
        assert result == expected
        mock_derive.assert_called_once_with("testorg", None)

    @mock.patch("github2gerrit.config.derive_gerrit_parameters")
    @mock.patch("github2gerrit.config._is_github_actions_context")
    def test_existing_values_not_overridden(self, mock_is_actions, mock_derive):
        """Test that existing non-empty values are not overridden."""
        mock_is_actions.return_value = False
        mock_derive.return_value = {
            "GERRIT_SSH_USER_G2G": "deriveduser",
            "GERRIT_SSH_USER_G2G_EMAIL": "derived@example.com",
        }

        cfg = {
            "GERRIT_SSH_USER_G2G": "existinguser",
            "GERRIT_SSH_USER_G2G_EMAIL": "",  # Empty, should be derived
        }

        result = apply_parameter_derivation(
            cfg, "testorg", save_to_config=False
        )

        expected = {
            "GERRIT_SSH_USER_G2G": "existinguser",  # Not overridden
            "GERRIT_SSH_USER_G2G_EMAIL": "derived@example.com",  # Derived
        }
        assert result == expected

    @mock.patch("github2gerrit.config.derive_gerrit_parameters")
    @mock.patch("github2gerrit.config._is_github_actions_context")
    def test_empty_values_are_derived(self, mock_is_actions, mock_derive):
        """Test that empty string values are treated as missing and derived."""
        mock_is_actions.return_value = False
        mock_derive.return_value = {
            "GERRIT_SSH_USER_G2G": "deriveduser",
            "GERRIT_SSH_USER_G2G_EMAIL": "derived@example.com",
        }

        cfg = {
            "GERRIT_SSH_USER_G2G": "   ",  # Whitespace only
            "GERRIT_SSH_USER_G2G_EMAIL": "",  # Empty
        }

        result = apply_parameter_derivation(
            cfg, "testorg", save_to_config=False
        )

        expected = {
            "GERRIT_SSH_USER_G2G": "deriveduser",
            "GERRIT_SSH_USER_G2G_EMAIL": "derived@example.com",
        }
        assert result == expected


class TestConfigIntegration:
    """Integration tests for configuration with SSH config parsing."""

    def setup_method(self):
        """Clear caches before each test to ensure isolation."""
        clear_credential_cache()

    def test_real_world_lfit_scenario(self, tmp_path):
        """Test real-world scenario with lfit organization configuration."""
        # Create a temporary SSH config
        ssh_config_content = """
Host gerrit.*
    User gerritbot
    Port 29418

Host *
    User defaultuser
"""
        ssh_config_file = tmp_path / "ssh_config"
        ssh_config_file.write_text(ssh_config_content)

        # Create organization config
        org_config_content = """
[exampleorg]
GERRIT_SERVER = "gerrit.example.org"
"""
        org_config_file = tmp_path / "org_config.txt"
        org_config_file.write_text(org_config_content)

        with (
            mock.patch.dict(
                os.environ,
                {
                    "G2G_CONFIG_PATH": str(org_config_file),
                    "G2G_RESPECT_USER_SSH": "true",
                },
            ),
            mock.patch(
                "github2gerrit.ssh_config_parser.Path.home"
            ) as mock_home,
        ):
            mock_home.return_value = tmp_path.parent

            # Set up SSH config path
            ssh_dir = tmp_path.parent / ".ssh"
            ssh_dir.mkdir(exist_ok=True)
            (ssh_dir / "config").write_text(ssh_config_content)

            with mock.patch(
                "github2gerrit.ssh_config_parser.get_git_user_email"
            ) as mock_git:
                mock_git.return_value = "user@example.org"

                # Test the full integration
                cfg = {}
                result = apply_parameter_derivation(
                    cfg, "exampleorg", save_to_config=False
                )

                expected = {
                    "GERRIT_SSH_USER_G2G": "gerritbot",
                    "GERRIT_SSH_USER_G2G_EMAIL": "user@example.org",
                    "GERRIT_SERVER": "gerrit.example.org",
                }
                assert result == expected

    def test_fallback_for_unknown_org(self, tmp_path):
        """Test fallback behavior for unknown organization."""
        # Empty SSH config
        ssh_config_file = tmp_path / "ssh_config"
        ssh_config_file.write_text("")

        with mock.patch(
            "github2gerrit.ssh_config_parser.Path.home"
        ) as mock_home:
            mock_home.return_value = tmp_path.parent
            ssh_dir = tmp_path.parent / ".ssh"
            ssh_dir.mkdir(exist_ok=True)
            (ssh_dir / "config").write_text("")

            with mock.patch(
                "github2gerrit.ssh_config_parser.get_git_user_email"
            ) as mock_git:
                mock_git.return_value = None

                cfg = {}
                result = apply_parameter_derivation(
                    cfg, "unknownorg", save_to_config=False
                )

                expected = {
                    "GERRIT_SSH_USER_G2G": "unknownorg.gh2gerrit",
                    "GERRIT_SSH_USER_G2G_EMAIL": "releng+unknownorg-gh2gerrit@linuxfoundation.org",
                    "GERRIT_SERVER": "gerrit.unknownorg.org",
                }
                assert result == expected

    def test_mixed_configuration_sources(self, tmp_path):
        """Test mixed configuration from SSH config, git config, and organization config."""
        # SSH config with partial coverage
        ssh_config_content = """
Host git.upstream.org
    User customuser
    Port 29418

Host *
    User defaultuser
"""
        ssh_config_file = tmp_path / "ssh_config"
        ssh_config_file.write_text(ssh_config_content)

        # Organization config with server override
        org_config_content = """
[testorg]
GERRIT_SERVER = "gerrit.custom.org"
"""
        org_config_file = tmp_path / "org_config.txt"
        org_config_file.write_text(org_config_content)

        with (
            mock.patch.dict(
                os.environ,
                {
                    "G2G_CONFIG_PATH": str(org_config_file),
                    "G2G_RESPECT_USER_SSH": "true",
                },
            ),
            mock.patch(
                "github2gerrit.ssh_config_parser.Path.home"
            ) as mock_home,
        ):
            mock_home.return_value = tmp_path.parent

            ssh_dir = tmp_path.parent / ".ssh"
            ssh_dir.mkdir(exist_ok=True)
            (ssh_dir / "config").write_text(ssh_config_content)

            with mock.patch(
                "github2gerrit.ssh_config_parser.get_git_user_email"
            ) as mock_git:
                mock_git.return_value = "user@custom.org"

                cfg = {}
                result = apply_parameter_derivation(
                    cfg, "testorg", save_to_config=False
                )

                expected = {
                    "GERRIT_SSH_USER_G2G": "defaultuser",  # From SSH config wildcard
                    "GERRIT_SSH_USER_G2G_EMAIL": "user@custom.org",  # From git config
                    "GERRIT_SERVER": "gerrit.custom.org",  # From org config
                }
                assert result == expected


class TestConfigurationPrecedence:
    """Test configuration precedence and override behavior."""

    @mock.patch("github2gerrit.config.load_org_config")
    @mock.patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    def test_explicit_config_overrides_ssh_config(
        self, mock_derive, mock_load_config
    ):
        """Test that explicit configuration overrides SSH config derivation."""
        mock_load_config.return_value = {}
        mock_derive.return_value = ("sshuser", "ssh@example.com")

        cfg = {
            "GERRIT_SSH_USER_G2G": "explicituser",  # Explicitly set
        }

        result = apply_parameter_derivation(
            cfg, "testorg", save_to_config=False
        )

        # Should keep explicit user but derive email
        expected = {
            "GERRIT_SSH_USER_G2G": "explicituser",
            "GERRIT_SSH_USER_G2G_EMAIL": "ssh@example.com",
            "GERRIT_SERVER": "gerrit.testorg.org",
        }
        assert result == expected

    @mock.patch("github2gerrit.config.load_org_config")
    @mock.patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    def test_organization_config_affects_ssh_lookup(
        self, mock_derive, mock_load_config
    ):
        """Test that organization config server affects SSH config lookup."""
        mock_load_config.return_value = {
            "GERRIT_SERVER": "custom.gerrit.example.com"
        }
        mock_derive.return_value = ("customuser", "custom@example.com")

        cfg = {}
        result = apply_parameter_derivation(
            cfg, "testorg", save_to_config=False
        )

        # Should use custom server for SSH lookup
        mock_derive.assert_called_once_with(
            "custom.gerrit.example.com", "testorg"
        )

        expected = {
            "GERRIT_SSH_USER_G2G": "customuser",
            "GERRIT_SSH_USER_G2G_EMAIL": "custom@example.com",
            "GERRIT_SERVER": "custom.gerrit.example.com",
        }
        assert result == expected


@pytest.fixture
def mock_config_environment():
    """Fixture to mock configuration file environment."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_dir = Path(tmp_dir)
        config_file = config_dir / "config.txt"

        with mock.patch.dict(os.environ, {"G2G_CONFIG_PATH": str(config_file)}):
            yield config_dir, config_file
