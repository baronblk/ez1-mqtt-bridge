"""Smoke test — verifies that the package imports cleanly and exposes its version."""

import re

import ez1_bridge


def test_package_imports() -> None:
    """The package can be imported and exposes ``__version__``."""
    assert hasattr(ez1_bridge, "__version__")


def test_version_is_pep440_like() -> None:
    """``__version__`` follows a basic ``X.Y.Z[suffix]`` shape."""
    assert re.match(r"^\d+\.\d+\.\d+", ez1_bridge.__version__)
