"""Pytest configuration and fixtures."""

from typing import Optional
import pytest

from gym.isabelle_gym import IsabelleGym
from repl.src.python.isabelle_repl import IsabelleRepl


# pylint: disable=too-few-public-methods
class SingletonManager:
    """Manager for singleton instances used in tests."""

    repl: Optional[IsabelleRepl] = None
    gym: Optional[IsabelleGym] = None


@pytest.fixture(scope="function")
def repl() -> IsabelleRepl:
    """Fixture to create a fresh REPL for testing."""
    if SingletonManager.repl is None:
        SingletonManager.repl = IsabelleRepl()
    else:
        SingletonManager.repl.reset()
    return SingletonManager.repl


@pytest.fixture(scope="function")
def gym() -> IsabelleGym:
    """Fixture to create a fresh IsabelleGym environment for testing."""
    if SingletonManager.gym is None:
        SingletonManager.gym = IsabelleGym()
    else:
        SingletonManager.gym.reset()
    return SingletonManager.gym
