"""Phase 0 placeholder check. Developer 1 replaces this file when prompt A1 starts.

The optimizer does not exist yet. This test pins that fact: the placeholder must
raise instead of returning plausible-looking numbers.
"""

import pytest

import optimizer


def test_optimizer_placeholder_fails_honestly():
    assert optimizer.STATUS == "not_implemented"
    with pytest.raises(optimizer.OptimizerNotImplementedError):
        optimizer.optimize({})
