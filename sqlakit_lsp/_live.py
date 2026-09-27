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


class _Example:
    """A value to render a template with: any path reads another of it."""

    def __getattr__(self, _: str) -> _Example:
        return self

    def __getitem__(self, _: object) -> _Example:
        return self

    def __iter__(self) -> Any:  # noqa: ANN401
        return iter((self,))

    def __len__(self) -> int:
        return 1

    def __str__(self) -> str:
        return "x"


def _module(path: Path) -> tuple[Path, str]:
    """Return the directory a module is imported from, and its dotted name."""
    parts = [path.stem]
    folder = path.parent
    while (folder / "__init__.py").is_file():
        parts.append(folder.name)
        folder = folder.parent
    return folder, ".".join(reversed(parts))


def _preparer(dialect: str) -> Any:  # noqa: ANN401
    import sqlalchemy as sa  # noqa: PLC0415
    import sqlalchemy.engine.default  # noqa: PLC0415
    import sqlalchemy.exc  # noqa: PLC0415

    try:
        return sa.engine.make_url(f"{dialect}://").get_dialect()().identifier_preparer
    except sa.exc.NoSuchModuleError:
        default = sa.engine.default.DefaultDialect()
        default.name = dialect
        return default.identifier_preparer


def main() -> None:
    request = json.load(sys.stdin)
    from sqlakit._project import load_project  # noqa: PLC0415
    from sqlakit._sql import Context, calls_kept, registered  # noqa: PLC0415

    project = load_project(Path(request["root"]))
    for file in request["modules"]:
        folder, name = _module(Path(file))
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
        try:
            found = registered([name])
        except Exception as error:  # noqa: BLE001 - the project's code raises anything
            sys.stdout.write(
                json.dumps({"error": f"{name} cannot be imported: {error}"}) + "\n"
            )
            return
        project.templates.macros.update(
            (key, macro) for key, macro in found.items() if key in request["macros"]
        )
    template = project.load(request["name"], request["source"])
    values: dict[str, Any] = dict.fromkeys(template.parameters(), _Example())
    values.update(dict.fromkeys(request["not_given"]))
    dialect = request["dialect"]
    with calls_kept():
        sql = template.render(Context(dialect, _preparer(dialect), values)).strip()
    sys.stdout.write(json.dumps({"sql": sql}) + "\n")


if __name__ == "__main__":
    main()
