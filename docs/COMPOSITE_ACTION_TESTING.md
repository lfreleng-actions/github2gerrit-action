<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2025 The Linux Foundation -->

# Composite Action Test Coverage

This document describes the comprehensive test suite for the GitHub Composite Action
(`action.yaml`) that validates all aspects of the action's behavior, configuration,
and integration.

## Overview

The composite action test suite consists of **83 tests** across 4 test modules that
provide complete coverage of the action's functionality:

- `test_composite_action_coverage.py` - 33 tests
- `test_action_environment_mapping.py` - 15 tests
- `test_action_step_validation.py` - 21 tests
- `test_action_outputs.py` - 14 tests

## Test Categories

### 1. Input Validation (`test_composite_action_coverage.py`)

**TestInputValidation** - Validates action input definitions and defaults

- Required input validation (GERRIT_SSH_PRIVKEY_G2G)
- Input default value verification
- Boolean input configuration

**TestPRNumberHandling** - Tests PR number processing logic

- Workflow dispatch with PR_NUMBER=0 (bulk mode)
- Workflow dispatch with specific PR numbers
- Invalid PR number validation
- Non-dispatch event PR number handling

**TestIssueIdLookup** - Validates Issue ID lookup functionality

- Conditional step execution for Issue ID lookup
- Priority handling (input vs resolved Issue ID)

### 2. Environment Variable Mapping (`test_action_environment_mapping.py`)

**TestActionEnvironmentMapping** - Validates input-to-environment mapping

- All inputs mapped to environment variables
- GitHub context variable mapping
- Computed environment variables from previous steps

**TestEnvironmentVariableProcessing** - Tests environment processing logic

- PR_NUMBER environment handling
- Issue ID environment resolution

**TestEnvironmentValidation** - Validates required environment variables

- Essential variables presence validation
- Boolean environment variable mapping
- Optional environment variable handling

**TestEnvironmentDefaults** - Tests default value configuration

- Input default values verification
- GitHub context defaults
- Port number and other specific defaults

**TestEnvironmentSecrets** - Validates secret handling

- Secret variable mapping validation
- No hardcoded secrets verification

### 3. Step Validation (`test_action_step_validation.py`)

**TestActionStepValidation** - Validates step execution and configuration

- Step execution order requirements
- Conditional step execution logic
- External action version pinning
- Shell step configuration and error handling
- Individual step configuration (Python, UV, checkout, etc.)

**TestStepIntegration** - Tests step integration

- Environment variable flow between steps
- GitHub output flow
- Step failure propagation

**TestActionValidationScripts** - Tests embedded validation scripts

- PR number validation script logic
- PR number normalization logic
- PR context extraction logic

**TestActionErrorHandling** - Validates error handling

- Script error handling (`set -euo pipefail`)
- Undefined variable handling
- Pipeline failure handling

**TestActionIntegrationScenarios** - Tests complete workflows

- Full workflow simulation
- Error scenario simulation

### 4. Output Handling (`test_action_outputs.py`)

**TestActionOutputs** - Validates output definitions

- Output definition correctness
- Step reference validation
- Multiline output support

**TestOutputCaptureStep** - Tests output capture implementation

- Capture step configuration
- Output capture script validation
- Output formatting logic
- Empty output handling

**TestMultilineOutputHandling** - Tests multiline outputs

- Multiline URL output handling
- Multiline change number handling
- Multiline commit SHA handling

**TestOutputErrorHandling** - Validates output error scenarios

- Best-effort capture behavior
- Permission error handling

**TestOutputIntegration** - Tests output integration

- Output consumption by following steps
- GitHub Actions output format compatibility

## Test Infrastructure

### CompositeActionTester Class

The test suite uses a specialized `CompositeActionTester` class that:

1. **Parses action.yaml** - Loads and validates the action configuration
2. **Extracts shell scripts** - Isolates shell scripts from action steps
3. **Simulates GitHub Actions environment** - Sets up proper environment variables
4. **Executes step scripts** - Runs individual step scripts in isolation
5. **Handles GitHub Actions expressions** - Substitutes `${{ }}` expressions

### Environment Simulation

Tests properly simulate the GitHub Actions runtime environment:

- `GITHUB_ENV` - For step-to-step environment variable passing
- `GITHUB_OUTPUT` - For action output generation
- Input mapping to `INPUT_*` environment variables
- GitHub context mapping to `GITHUB_*` environment variables

### Error Handling Validation

The test suite validates proper error handling:

- Scripts use `set -euo pipefail` for robust error handling
- Validation steps return appropriate exit codes
- Error messages are properly formatted and informative

## Security Testing

**TestSecurityConsiderations** validates security aspects:

- No hardcoded secrets in action configuration
- Proper SSH key input handling
- GitHub token usage from context
- Sensitive pattern detection

## Integration Testing

**TestIntegrationScenarios** covers real-world usage:

- Pull request workflow simulation
- Dry run capability testing
- CI testing mode validation
- Performance considerations (caching, UV package manager)

## Running the Tests

```bash
# Run all action tests
pytest tests/test_composite_action_coverage.py tests/test_action_environment_mapping.py tests/test_action_step_validation.py tests/test_action_outputs.py -v

# Run specific test categories
pytest tests/test_composite_action_coverage.py::TestPRNumberHandling -v
pytest tests/test_action_outputs.py::TestMultilineOutputHandling -v

# Run with coverage (note: these tests don't directly test Python source code)
pytest tests/test_composite_action_coverage.py --no-cov -v
```

## Test Coverage Goals

The composite action test suite ensures:

1. **Complete input validation** - All inputs undergo validation and default testing
2. **Environment mapping verification** - All variables properly mapped
3. **Step execution validation** - Correct order and configuration
4. **Output format compliance** - GitHub Actions output format adherence
5. **Error handling robustness** - Proper error propagation and handling
6. **Security compliance** - No hardcoded secrets or security issues
7. **Integration readiness** - Real-world usage scenario coverage

## Maintenance

When modifying `action.yaml`:

1. Update corresponding tests to match changes
2. Add new tests for new functionality
3. Verify that all test assertions match actual action behavior
4. Ensure security and error handling standards remain consistent

The test suite serves as both validation and documentation of the action's
expected behavior, ensuring reliability and maintainability of the composite action.
