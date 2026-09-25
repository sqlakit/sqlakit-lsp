"""The language server for SQLAKit templates.

`sqlakit-lsp` runs it over standard input and output, for an editor to start.
It reads the project the way `sqlakit check` does, without running its code.
"""

from ._server import serve

__all__ = ["serve"]
