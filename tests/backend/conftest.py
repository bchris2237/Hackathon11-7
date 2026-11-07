import pytest

from tests.backend.helpers import fixture


@pytest.fixture(scope="session")
def canonical():
    return fixture("canonical.json")
