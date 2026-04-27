"""Smoke test — verifies that the package imports cleanly and exposes its version."""

import re
from importlib import metadata

import ez1_bridge


def test_package_imports() -> None:
    """The package can be imported and exposes ``__version__``."""
    assert hasattr(ez1_bridge, "__version__")


def test_version_is_pep440_like() -> None:
    """``__version__`` follows a basic ``X.Y.Z[suffix]`` shape."""
    assert re.match(r"^\d+\.\d+\.\d+", ez1_bridge.__version__)


def test_version_pinned_to_release() -> None:
    """``__version__`` matches the pinned release; guards against the
    pyproject/__init__ pair drifting apart on the next bump."""
    # Pinning the runtime constant keeps `python -m ez1_bridge --version`
    # honest after a release tag, and would have caught the v0.1.0 cut
    # being prepared while metadata still claimed 0.0.0.
    assert ez1_bridge.__version__ == "0.1.2"
    assert metadata.version("ez1-mqtt-bridge") == ez1_bridge.__version__
