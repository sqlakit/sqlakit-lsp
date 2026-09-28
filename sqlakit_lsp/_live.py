"""Render a template with the project's own macros, in the project's Python.

The server reads the project's code and never runs it, so a macro written in
Python stays a call when it renders a template. **Show rendered SQL** asks for
more: the server runs this file with the project's interpreter, which imports
the modules of those macros, and prints the SQL as JSON.

It reads its request as JSON on stdin, and imports nothing of the server: the
project's environment has `sqlakit`, and not the server.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from sqlakit._project import load_project
from sqlakit._render import Example, _preparer, import_macros
from sqlakit._sql import _rendered, calls_kept


def main() -> None:
    request = json.load(sys.stdin)
    project = load_project(Path(request["root"]))
    try:
        import_macros(project)
    except Exception as error:  # noqa: BLE001 - the project's code raises anything
        problem = (
            f"the project's macros cannot be imported: {type(error).__name__}: {error}"
        )
        sys.stdout.write(json.dumps({"error": problem}) + "\n")
        return
    template = project.load(request["name"], request["source"])
    values: dict[str, Any] = dict.fromkeys(template.parameters(), Example())
    values.update(dict.fromkeys(request["not_given"]))
    dialect = request["dialect"]
    with calls_kept():
        sql, _ = _rendered(template, {"dialect": dialect, **values}, _preparer(dialect))
    sys.stdout.write(json.dumps({"sql": sql.strip()}) + "\n")


if __name__ == "__main__":
    main()
