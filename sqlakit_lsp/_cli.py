"""`sqlakit-lsp`, the command line."""

from __future__ import annotations

import argparse
from importlib.metadata import version

from ._server import serve


def main(argv: list[str] | None = None) -> int:
    """Serve an editor over standard input and output until it hangs up."""
    parser = argparse.ArgumentParser(
        prog="sqlakit-lsp",
        description="The language server for SQLAKit templates, over stdio.",
    )
    parser.add_argument("--version", action="version", version=version("sqlakit-lsp"))
    # Some editors pass it to every server: stdio is the only way this one talks.
    parser.add_argument("--stdio", action="store_true", help=argparse.SUPPRESS)
    parser.parse_args(argv)
    serve()  # pragma: no cover - run by an editor
    return 0  # pragma: no cover
