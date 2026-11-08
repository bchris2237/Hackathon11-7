"""
Phase 0 placeholder test — superseded by A1 implementation.
Now confirms that the solver is IMPLEMENTED (no longer raises NotImplementedError)
and that basic smoke checks pass.

Developer 1 owns this file. Full contract tests are in test_contract.py.
"""
import json
from pathlib import Path

import pytest

from tests.optimizer import _fixtures
from optimizer.contract import Scenario
from optimizer.solver import solve, verify

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


def _load_scenario(filename: str) -> Scenario:
    data = _fixtures.load(filename)
    return Scenario.model_validate(data["scenario"])


def test_solve_is_implemented():
    """A1: solver must NOT raise NotImplementedError any more."""
    scenario = _load_scenario("canonical.json")
    result = solve(scenario)  # should not raise
    assert result is not None
    assert result.schemaVersion == "1"


def test_verify_is_implemented():
    """A1: verify must NOT raise NotImplementedError any more."""
    scenario = _load_scenario("canonical.json")
    result = solve(scenario)
    verified = verify(scenario, result)
    assert verified is not None
