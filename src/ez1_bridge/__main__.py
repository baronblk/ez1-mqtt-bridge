"""Module entrypoint — enables ``python -m ez1_bridge``.

Implementation lands in Phase 6 (CLI subcommands: ``run``, ``probe``, ``--version``).
"""

from ez1_bridge.main import cli_entrypoint


def main() -> None:
    """Forward to :func:`ez1_bridge.main.cli_entrypoint`."""
    cli_entrypoint()


if __name__ == "__main__":
    main()
