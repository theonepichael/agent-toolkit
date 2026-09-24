#!/usr/bin/env python3
"""Resolve ``pytest`` for the dependency-free unittest-style test files.

The former ``agent-scripts/test_*.py`` suites are standard-library
``unittest`` and must stay runnable on a machine that has never run
``uv sync`` (no ``pytest`` in site-packages). Those files only ever use
``pytest.mark.*`` decorators, so when ``pytest`` is not importable we return
a generic shim whose ``mark.<any>`` attribute is an identity decorator
factory. It supports both the bare form ``@pytest.mark.foo`` and the called
form ``@pytest.mark.foo(...)``.

Each contract file imports it as::

    from pytest_shim import pytest

instead of a bare ``import pytest``.
"""

from __future__ import annotations


def _identity_mark(*args: object, **kwargs: object) -> object:
    """Act as either ``@mark.foo`` (bare) or ``@mark.foo(...)`` (factory)."""
    if len(args) == 1 and not kwargs and callable(args[0]):
        return args[0]

    def decorator(target: object) -> object:
        return target

    return decorator


class _MarkNamespace:
    """Any attribute access yields the shared identity-mark factory."""

    def __getattr__(self, _name: str) -> object:
        return _identity_mark


class _PytestShim:
    """Minimal stand-in for ``pytest`` when it is not installed."""

    def __init__(self) -> None:
        self.mark = _MarkNamespace()


def get_pytest() -> object:
    """Return the real ``pytest`` module, or the marker shim if unavailable."""
    try:
        import pytest
    except ModuleNotFoundError:
        return _PytestShim()
    return pytest


pytest = get_pytest()
