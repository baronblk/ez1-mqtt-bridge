"""Module entrypoint — enables ``python -m ez1_bridge``.

Wires :func:`ez1_bridge.main.cli_entrypoint` (which returns an exit code)
into :func:`sys.exit` so the process status reflects the CLI outcome.
"""

import sys

from ez1_bridge.main import cli_entrypoint


def main() -> None:
    """Forward to :func:`ez1_bridge.main.cli_entrypoint` and propagate exit code."""
    sys.exit(cli_entrypoint())


if __name__ == "__main__":
    main()
