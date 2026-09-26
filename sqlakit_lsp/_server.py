"""The language server for SQLAKit templates, run as `sqlakit-lsp`.

It reads the project the way `sqlakit check` does, from `pyproject.toml`, and
offers what an editor asks for while a template is written:

- **Problems** as you type: an unknown macro, a call with the wrong arguments,
  a string or a call never closed, an include that is missing or circular.
- **Completion** of macros after `tpl.`, of template names in
  `tpl.include('`, and of the parameters the file already uses after `:`.
- **Hover** on a macro: how a template calls it, and its docstring.
- **Definition** of a macro, in its Python module, and of an included template.
- **References**: every call of a macro, and everything that reads a template,
  an `include` or a `db.sql(...)`, asked from anywhere in the template.

`_Assistant` reads the text at an offset, and knows nothing of the protocol.
`serve` turns it into a server.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

import sqlalchemy as sa
import sqlalchemy.engine.default
import sqlalchemy.exc
from lsprotocol import types
from pygls.exceptions import JsonRpcException
from pygls.lsp.server import LanguageServer
from sqlakit._project import Project, load_project
from sqlakit._sql import (
    INCLUDE,
    Context,
    Macro,
    Param,
    SqlMacro,
    signature_of,
    sql_macros,
)
from sqlakit._static import SKIPPED, StaticMacro

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
from sqlakit.exceptions import (
    MacroArgumentError,
    MacroSyntaxError,
    ProjectConfigError,
    SQLAKitError,
    UnknownMacroError,
)

__all__ = ["Completion", "Diagnostic", "Target", "serve"]

_PARAMETERS = re.compile(r"(?<![:\w\\]):([A-Za-z_]\w*)")
_PARAMETER_TYPED = re.compile(r"(?<![:\w\\]):\w*$")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A problem in the text, from one offset to another."""

    start: int
    end: int
    message: str


@dataclass(frozen=True, slots=True)
class Completion:
    """One thing that can be written at the cursor."""

    label: str
    kind: str
    """`macro`, `template` or `parameter`."""
    detail: str = ""
    documentation: str = ""
    snippet: str | None = None
    """The text to insert, with `${1:placeholders}`, when it is not the label."""


@dataclass(frozen=True, slots=True)
class Target:
    """A definition's place: a file, and the line and column in it, from zero.

    The column counts characters, and the server gives it to the editor in the
    UTF-16 units the protocol counts in.
    """

    path: Path
    line: int
    column: int = 0


def _innermost(parts: Sequence[Any], offset: int) -> Any:  # noqa: ANN401
    """Return the innermost macro call of a read template that holds the offset."""
    for part in parts:
        if isinstance(part, str) or not part.span[0] <= offset <= part.span[1]:
            continue
        for argument in part.args:
            if (inner := _innermost(argument.parts, offset)) is not None:
                return inner
        return part
    return None


def _rendered(template: Any, dialect: str, values: dict[str, Any]) -> str | None:  # noqa: ANN401
    """Return the SQL a read template writes for these values, if it can."""
    try:
        return template.render(Context(dialect, _preparer(dialect), values)).strip()
    except Exception:  # noqa: BLE001 - a macro may want a value of its own kind
        return None


@lru_cache
def _preparer(dialect: str) -> Any:  # noqa: ANN401
    """Return how a dialect quotes names, loaded without a driver or a server."""
    try:
        return sa.engine.make_url(f"{dialect}://").get_dialect()().identifier_preparer
    except sa.exc.NoSuchModuleError:
        default = sa.engine.default.DefaultDialect()
        default.name = dialect
        return default.identifier_preparer


def _open_brackets(source: str, start: int, end: int) -> list[tuple[int, int]]:
    """Return each bracket still open at ``end``, and the commas written in it.

    Strings and `--` comments are passed over, so a comma in one counts for none.
    """
    opened: list[list[int]] = []
    index = start
    while index < end:
        char = source[index]
        if char in "'\"":
            closing = source.find(char, index + 1)
            index = end if closing < 0 else closing + 1
            continue
        if source.startswith("--", index):
            newline = source.find("\n", index)
            index = end if newline < 0 else newline + 1
            continue
        if char == "(":
            opened.append([index, 0])
        elif char == ")" and opened:
            opened.pop()
        elif char == "," and opened:
            opened[-1][1] += 1
        index += 1
    return [(bracket, commas) for bracket, commas in opened]


class RequestFailed(JsonRpcException):
    """A request the project cannot answer, such as a rename it cannot take.

    The editor shows the message.
    """

    CODE = -32803


@dataclass(frozen=True, slots=True)
class Signature:
    """A macro's call as a template writes it, and the argument being written."""

    label: str
    arguments: tuple[str, ...]
    active: int
    doc: str


@dataclass(frozen=True, slots=True)
class Reference:
    """A place that names a macro or a template: a file, and the text's span."""

    path: Path
    start: int
    end: int


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class _Scan:
    """Where a file calls each macro and names each template, by name."""

    stamp: int
    """The file's modification time when it was read, or 0 for unsaved text."""
    macros: dict[str, list[tuple[int, int]]]
    templates: dict[str, list[tuple[int, int]]]


_PYTHON_CALL = re.compile(r"(?<![\w.])tpl\.(\w+)")
"""A macro called from Python, through the `tpl` object: `tpl.icontains(...)`."""

_WORD = re.compile(r"\w+")


def _word_at(source: str, offset: int) -> tuple[int, int] | None:
    """Return the span of the word the offset is in or next to."""
    start = source.rfind("\n", 0, offset) + 1
    end = source.find("\n", offset)
    for found in _WORD.finditer(source, start, len(source) if end < 0 else end):
        if found.start() <= offset <= found.end():
            return found.span()
    return None


class _Assistant:
    """Read a template's text at an offset, for one project."""

    def __init__(self, project: Project) -> None:
        self.project = project
        namespace = re.escape(project.templates.namespace)
        self._name_at = re.compile(rf"(?<![\w.])({namespace})\.(\w+)", re.IGNORECASE)
        self._macro_typed = re.compile(rf"(?<![\w.]){namespace}\.(\w*)$", re.IGNORECASE)
        self._include_typed = re.compile(
            rf"(?<![\w.]){namespace}\.{INCLUDE}\(\s*'([^']*)$", re.IGNORECASE
        )
        self._include_path = re.compile(
            rf"(?<![\w.]){namespace}\.{INCLUDE}\(\s*'([^']*)'", re.IGNORECASE
        )
        self._macro_sql = {
            Path(sql).resolve()
            for macro in project.templates.macros.values()
            if (sql := getattr(macro, "sql_path", None)) is not None
        }
        """The files `@sql_macro("file.sql")` keeps its SQL in."""
        self._sources: set[Path] | None = None
        self._names: list[str] | None = None
        self._files: tuple[list[Path], list[Path]] | None = None
        self._scans: dict[Path, _Scan] = {}
        self._reads: dict[str, tuple[int, frozenset[str]]] = {}

    def keep(self, previous: _Assistant) -> None:
        """Take what another assistant of the project has read, where it still holds.

        What a file calls and names does not change with the macros, so the scans
        outlive a reload, while the namespace a template calls them in stays. The
        list of files stays while the templates and the files of macros do: a file
        made or removed is `forget_files`.
        """
        before, after = previous.project, self.project
        if before.templates.namespace == after.templates.namespace:
            self._scans = previous._scans
        if (
            tuple(before.templates.paths) == tuple(after.templates.paths)
            and before.macro_files() == after.macro_files()
            and previous._macro_sql == self._macro_sql
        ):
            self._names = previous._names
            self._files = previous._files

    def scan_all(self) -> Iterator[Path]:
        """Read each file of the project that references look in, one at a time."""
        sql_files, python_files = self._project_files()
        for path in [*sql_files, *python_files]:
            self._scan(path, None)
            yield path

    def lists(self, path: Path) -> bool:
        """Whether the list of files, if it was read, holds this one."""
        if self._files is None:
            return True
        resolved = path.resolve()
        return any(resolved in files for files in self._files)

    def forget_files(self) -> None:
        """Read the list of templates and Python files again on the next request."""
        self._names = self._files = None

    def names(self) -> list[str]:
        """Every template's name, read once until `forget_files`."""
        if self._names is None:
            self._names = self.project.templates.names()
        return self._names

    def defines_macros(self, path: Path) -> bool:
        """Whether a file defines a macro the project calls, its own or built in.

        The library's own file is one: references on `def identifier` there are
        the calls of `tpl.identifier` in the templates.
        """
        if self._sources is None:
            self._sources = {
                target.path.resolve()
                for macro in self.project.templates.macros.values()
                if (target := _source_of(macro)) is not None
            }
        return path.resolve() in self._sources

    def applies_to(self, path: Path) -> bool:
        """Whether a file is a template of this project, or holds its macros."""
        name = self.project.name_of(path)
        return (
            (name is not None and name.endswith(".sql"))
            or self._reads_macros(path)
            or path.resolve() in self._macro_sql
        )

    def diagnose(self, path: Path, source: str) -> list[Diagnostic]:
        """Return what is wrong with the text: the first problem, where it is."""
        if self._reads_macros(path):
            return self._diagnose_macros(path, source)
        if path.resolve() in self._macro_sql:
            return []
        name = self.project.name_of(path) or path.name
        try:
            self.project.load(name, source)
        except (MacroSyntaxError, UnknownMacroError, MacroArgumentError) as error:
            return [self._placed(error, name, source)]
        return [Diagnostic(*found) for found in self.project.foreign_calls(source)]

    def holds_macros(self, path: Path) -> bool:
        """Whether a file defines macros: SQL macros, or the SQL of a file macro."""
        return self._reads_macros(path) or path.resolve() in self._macro_sql

    def _reads_macros(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(
            isinstance(macro, SqlMacro) and macro.path.resolve() == resolved
            for macro in self.project.templates.macros.values()
        )

    def _diagnose_macros(self, path: Path, source: str) -> list[Diagnostic]:
        """Return what is wrong with each macro of a file of SQL macros."""
        return [
            *(
                Diagnostic(*_line_span(source, line), message)
                for line, message in self.project.macro_problems(path, source)
            ),
            *(Diagnostic(*found) for found in self.project.foreign_calls(source)),
        ]

    def complete(self, source: str, offset: int) -> list[Completion]:
        """Return what can be written at the offset, given what is typed before it."""
        typed = source[source.rfind("\n", 0, offset) + 1 : offset]
        if match := self._include_typed.search(typed):
            return self._templates(match.group(1))
        if match := self._macro_typed.search(typed):
            return self._macros(match.group(1).lower())
        if _PARAMETER_TYPED.search(typed):
            return self._parameters(source)
        return []

    def hover(self, source: str, offset: int) -> str | None:
        """Return how the macro under the offset is called, and its docstring."""
        name = self._macro_at(source, offset)
        if name == INCLUDE:
            return (
                f"```sql\n{self.project.templates.namespace}.{INCLUDE}('path.sql')"
                "\n```\n\nThe query of another template, in parentheses, where a "
                "table goes. It shares the parameters of the call."
            )
        macro = self.project.templates.macros.get(name or "")
        if macro is None:
            return None
        signature = signature_of(macro, self.project.templates.namespace)
        written = self.written(source, offset)
        if not written:
            return f"```sql\n{signature}\n```\n\n{macro.doc}".rstrip()
        return f"{written}\n\n```sql\n{signature}\n```\n\n{macro.doc}".rstrip()

    def written(self, source: str, offset: int) -> str:
        """Return the SQL the call under the offset writes, as Markdown.

        It is written on the project's dialect: once with every parameter it
        reads given, and once with none, when the two differ. Empty when the call
        cannot be written without real values.
        """
        try:
            template = self.project.load("<hover>", source)
        except SQLAKitError:
            return ""
        call = _innermost(template.parts, offset)
        if call is None:
            return ""
        text = source[call.span[0] : call.span[1]]
        try:
            alone = self.project.load("<hover>", text)
        except SQLAKitError:
            return ""
        names = sorted(alone.parameters())
        dialect = self.project.dialect or "postgresql"
        given = _rendered(alone, dialect, dict.fromkeys(names, "x"))
        missing = _rendered(alone, dialect, dict.fromkeys(names))
        if not names or given == missing:
            one = given or missing
            return f"On {dialect}:\n\n```sql\n{one}\n```" if one else ""
        listed = ", ".join(f"`:{name}`" for name in names)
        # A block of SQL alone, each, so an editor highlights it as SQL.
        return "\n\n".join(
            f"{listed} {caption}:\n\n```sql\n{sql}\n```"
            for caption, sql in (("given", given), ("not given", missing))
            if sql
        )

    def signature(self, source: str, offset: int) -> Signature | None:
        """Return the macro whose arguments the offset is in, and which one it is."""
        start = max(0, source.rfind("\n\n", 0, offset))
        opened = _open_brackets(source, start, offset)
        for bracket, commas in reversed(opened):
            called = self._macro_typed.search(source, start, bracket)
            if called is None or called.end() != bracket:
                continue
            macro = self.project.templates.macros.get(called.group(1).lower())
            if macro is None:
                return None
            names = [
                f":{slot.name}" if slot.kind is Param else slot.name
                for slot in macro.slots
            ]
            if macro.variadic is not None:
                variadic = macro.variadic
                names.append(f"*{':' if variadic.kind is Param else ''}{variadic.name}")
            active = min(commas, len(names) - 1) if names else 0
            namespace = self.project.templates.namespace
            label = f"{namespace}.{macro.name}({', '.join(names)})"
            return Signature(label, tuple(names), active, macro.doc)
        return None

    def definition(
        self, source: str, offset: int, path: Path | None = None
    ) -> Target | None:
        """Return where the macro or the included template under the offset is.

        On a macro's own name where it is defined, that is the name itself, and
        an editor then lists what calls it. In the file `@sql_macro("file.sql")`
        keeps its SQL in, it is the function.
        """
        start = source.rfind("\n", 0, offset) + 1
        end = source.find("\n", offset)
        line = source[start : len(source) if end < 0 else end]
        for match in self._include_path.finditer(line):
            if match.start(1) <= offset - start <= match.end(1):
                path = self.project.path_of(match.group(1))
                return None if path is None else Target(path, 0)
        macro = self.project.templates.macros.get(self._macro_at(source, offset) or "")
        if macro is not None:
            return _source_of(macro)
        if path is None:
            return None
        # The SQL of `@sql_macro("file.sql")` goes to its function.
        return self.implementation(path, source, offset) or self._itself(
            path, source, offset
        )

    def implementation(self, path: Path, source: str, offset: int) -> Target | None:
        """Return the other half of a macro whose SQL is in a file.

        `@sql_macro("tenant.sql")` has a function and a statement: from the
        statement's name this is the function, and from the function's name it
        is the statement. Anything else goes where `definition` goes.
        """
        name = self._defined_at(path, source, offset)
        macro = self.project.templates.macros.get(name or "")
        sql = getattr(macro, "sql_path", None)
        if macro is None or sql is None:
            return None
        if Path(sql).resolve() == path.resolve():
            return _source_of(macro)
        statement = next(
            (one for one in sql_macros(Path(sql)) if one.name == macro.name), None
        )
        if statement is None:
            return None
        line, column = statement.name_at
        return Target(Path(sql), line - 1, column)

    def _itself(self, path: Path, source: str, offset: int) -> Target | None:
        """Return the name under the offset as a place, when it defines a macro."""
        if self._defined_at(path, source, offset) is None:
            return None
        start, _ = _word_at(source, offset) or (offset, offset)
        line = source.count("\n", 0, start)
        return Target(path, line, start - (source.rfind("\n", 0, start) + 1))

    def origin(
        self,
        source: str,
        offset: int,
        *,
        python: bool = False,
        path: Path | None = None,
    ) -> tuple[int, int] | None:
        """Return the span of the name a definition starts from.

        An editor underlines it: `tpl.active`, the path in `tpl.include('...')`,
        the name in `db.sql("...")`, or a macro's name where it is defined.
        """
        defined = (
            _word_at(source, offset)
            if path is not None and self._defined_at(path, source, offset)
            else None
        )
        if python:
            return next(
                (
                    (start, end)
                    for start, end, _ in _template_names(source)
                    if start <= offset <= end
                ),
                defined,
            )
        start = source.rfind("\n", 0, offset) + 1
        end = source.find("\n", offset)
        end = len(source) if end < 0 else end
        for match in self._include_path.finditer(source, start, end):
            if match.start(1) <= offset <= match.end(1):
                return match.span(1)
        for match in self._name_at.finditer(source, start, end):
            if match.start() <= offset <= match.end():
                return match.span()
        return defined

    def reads_python(self, path: Path) -> bool:
        """Whether a file is Python of this project, which names templates."""
        if path.suffix != ".py":
            return False
        try:
            relative = path.resolve().relative_to(self.project.root.resolve())
        except ValueError:
            return False
        return not any(part in SKIPPED for part in relative.parts[:-1])

    def python_diagnose(self, source: str) -> list[Diagnostic]:
        """Return what is wrong with the templates the code reads.

        That is a template no template directory holds, and a value a call
        passes by name that its template does not read.
        """
        where = ", ".join(
            _relative(Path(root), self.project.root)
            for root in self.project.templates.paths
        )
        found = [
            Diagnostic(start, end, f"No SQL template named `{name}` in {where}.")
            for start, end, name in _template_names(source)
            if self.project.path_of(name) is None
        ]
        for call in _template_calls(source):
            reads = None if call.open else self.parameters(call.name)
            if reads is None:
                continue
            listed = ", ".join(f"`{name}`" for name in sorted(reads)) or "nothing"
            found.extend(
                Diagnostic(
                    start,
                    end,
                    f"`{name}` is not a parameter of `{call.name}`, which reads {listed}.",
                )
                for name, start, end in call.keywords
                if name not in reads
            )
        return found

    def parameters(self, name: str) -> frozenset[str] | None:
        """Return the parameters a template reads, or None when it cannot be read.

        Read once for each time its file changes.
        """
        path = self.project.path_of(name)
        if path is None:
            return None
        stamp = path.stat().st_mtime_ns
        cached = self._reads.get(name)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            reads = self.project.load(name, _read(path)).parameters()
        except SQLAKitError:
            return None
        self._reads[name] = (stamp, reads)
        return reads

    def python_complete(self, source: str, offset: int) -> list[Completion]:
        """Return the template names that can go where the code reads one."""
        typed = source[source.rfind("\n", 0, offset) + 1 : offset]
        if match := _TEMPLATE_TYPED.search(typed):
            return [
                Completion(name, "template")
                for name in self.names()
                if name.startswith(match.group(1))
            ]
        if match := _ARGUMENT_TYPED.search(source, 0, offset):
            reads = self.parameters(match.group("name")) or frozenset()
            passed = set(_PASSED.findall(match.group("passed")))
            return [
                Completion(name, "parameter", snippet=f"{name}=")
                for name in sorted(reads - passed)
                if name.startswith(match.group("typed"))
            ]
        return []

    def python_definition(
        self, source: str, offset: int, path: Path | None = None
    ) -> Target | None:
        """Return the template the name under the offset reads.

        On the name of a `def` that defines a macro, that is the name itself.
        """
        for start, end, name in _template_names(source):
            if start <= offset <= end:
                found = self.project.path_of(name)
                return None if found is None else Target(found, 0)
        return None if path is None else self._itself(path, source, offset)

    def links(self, path: Path, source: str) -> list[tuple[int, int, Path]]:
        """Return each template the text names, where the name is, and its file."""
        if self.reads_python(path):
            named = _template_names(source)
        else:
            named = [
                (found.start(1), found.end(1), found.group(1))
                for found in self._include_path.finditer(source)
            ]
        return [
            (start, end, target)
            for start, end, name in named
            if (target := self.project.path_of(name)) is not None
        ]

    def references(
        self,
        path: Path,
        source: str,
        offset: int,
        held: Mapping[Path, str] | None = None,
    ) -> list[Reference]:
        """Return every place that names what is under the offset.

        Under a macro, its call or its definition, the places are its calls:
        in the templates, in the files of macros, and in Python through `tpl`.
        Under a template's name, in `tpl.include('...')` or in `db.sql("...")`,
        they are what reads that template. Anywhere else in a template, they are
        what reads the template itself. ``held`` is the text of the files the
        editor has open, which may not be saved.
        """
        held = held or {}
        if self.reads_python(path):
            for start, end, name in _template_names(source):
                if start <= offset <= end:
                    return self._references(held, templates=name)
            macro = self._defined_at(path, source, offset)
            return [] if macro is None else self._references(held, macro=macro)
        macro = self._macro_at(source, offset) or self._defined_at(path, source, offset)
        if macro is not None and macro != INCLUDE:
            return self._references(held, macro=macro)
        for found in self._include_path.finditer(source):
            if found.start() <= offset <= found.end():
                return self._references(held, templates=found.group(1))
        name = self.project.name_of(path)
        if name is None or self._reads_macros(path):
            return []
        return self._references(held, templates=name)

    def renamable(self, path: Path, source: str, offset: int) -> tuple[int, int] | str:
        """Return the span a rename starts from, or why nothing there renames."""
        for start, end, _ in self._names_in(path, source):
            if start <= offset <= end:
                return start, end
        name = self.macro_named(path, source, offset)
        if name is None:
            return "Rename a macro, or the name of a template in a call that reads it."
        problem = self._definitions(name)
        if isinstance(problem, str):
            return problem
        span = _word_at(source, offset)
        return span if span is not None else (offset, offset)

    def rename(
        self,
        path: Path,
        source: str,
        offset: int,
        new: str,
        held: Mapping[Path, str] | None = None,
    ) -> tuple[list[Reference], tuple[Path, Path] | None] | str:
        """Return what a rename changes, or why it cannot be done.

        That is each span to write the new name into, and a template's file to
        move from one path to the other.
        """
        held = held or {}
        for start, end, name in self._names_in(path, source):
            if start <= offset <= end:
                return self._rename_template(name, new, held)
        name = self.macro_named(path, source, offset)
        if name is None:
            return "Rename a macro, or the name of a template in a call that reads it."
        return self._rename_macro(name, new, held)

    def _names_in(self, path: Path, source: str) -> list[tuple[int, int, str]]:
        """Return each template the text names, and where the name is."""
        if self.reads_python(path):
            return _template_names(source)
        return [
            (found.start(1), found.end(1), found.group(1))
            for found in self._include_path.finditer(source)
        ]

    def _rename_template(
        self, name: str, new: str, held: Mapping[Path, str]
    ) -> tuple[list[Reference], tuple[Path, Path] | None] | str:
        old = self.project.path_of(name)
        if old is None:
            return f"No SQL template named `{name}`."
        if not new.endswith(".sql"):
            return f"A template's name ends in `.sql`: `{new}` does not."
        if self.project.path_of(new) is not None:
            return f"`{new}` is a template already."
        root = next(
            Path(root)
            for root in self.project.templates.paths
            if old.resolve().is_relative_to(Path(root).resolve())
        )
        return self._references(held, templates=name), (old, root / new)

    def _rename_macro(
        self, name: str, new: str, held: Mapping[Path, str]
    ) -> tuple[list[Reference], tuple[Path, Path] | None] | str:
        definitions = self._definitions(name)
        if isinstance(definitions, str):
            return definitions
        if not re.fullmatch(r"[A-Za-z_]\w*", new):
            return (
                f"A macro's name is a word of letters, digits and `_`: `{new}` is not."
            )
        if new.lower() in self.project.templates.macros or new.lower() == INCLUDE:
            return f"`{new}` is a macro already."
        return [*definitions, *self._references(held, macro=name)], None

    def _definitions(self, name: str) -> list[Reference] | str:
        """Return where a macro of the project names itself, or why it cannot be renamed.

        That is the `def` of a Python macro, the `AS name` of an SQL one, and both
        for `@sql_macro("file.sql")`.
        """
        macro = self.project.templates.macros.get(name)
        if not isinstance(macro, StaticMacro | SqlMacro):
            return f"`{name}` is built in, and keeps its name."
        found = []
        declared = self.declaration(name)
        if declared is not None:
            found.append(declared)
        sql = getattr(macro, "sql_path", None)
        if sql is not None:
            statement = next(
                (one for one in sql_macros(Path(sql)) if one.name == name), None
            )
            if statement is not None:
                text = _read(Path(sql))
                line, column = statement.name_at
                start = _line_span(text, line)[0] + column
                found.append(Reference(Path(sql), start, start + len(name)))
        for place in found:
            written = _read(place.path)[place.start : place.end]
            if written.lower() != name:
                return (
                    f"`{name}` is named in its decorator, not by its function: "
                    f"rename it there"
                )
        return found

    def declaration(self, name: str) -> Reference | None:
        """Return where a macro's name is written, for a list of its references."""
        macro = self.project.templates.macros.get(name)
        if macro is None or (target := _source_of(macro)) is None:
            return None
        text = _read(target.path)
        start = _line_span(text, target.line + 1)[0] + target.column
        return Reference(target.path, start, start + len(macro.name))

    def macro_named(self, path: Path, source: str, offset: int) -> str | None:
        """Return the macro under the offset: at a call, or where it is defined."""
        if not self.reads_python(path):
            called = self._macro_at(source, offset)
            if called is not None and called != INCLUDE:
                return called
        return self._defined_at(path, source, offset)

    def _defined_at(self, path: Path, source: str, offset: int) -> str | None:
        """Return the macro whose name the offset is on, where it is defined.

        That is the line of its `def` or its `AS name`, or anywhere in the file
        `@sql_macro("file.sql")` keeps its SQL in.
        """
        line = source.count("\n", 0, offset) + 1
        span = _word_at(source, offset)
        word = None if span is None else source[span[0] : span[1]].lower()
        macro = self.project.templates.macros.get(word or "")
        if macro is None:
            return None
        sql = getattr(macro, "sql_path", None)
        if sql is not None and Path(sql).resolve() == path.resolve():
            return word
        target = _source_of(macro)
        if target is None or target.path.resolve() != path.resolve():
            return None
        return word if target.line + 1 == line else None

    def _references(
        self,
        held: Mapping[Path, str],
        *,
        macro: str | None = None,
        templates: str | None = None,
    ) -> list[Reference]:
        """Return the calls of a macro, or what reads a template, in every file."""
        sql_files, python_files = self._project_files()
        found = []
        for path in [*sql_files, *python_files]:
            scan = self._scan(path, held.get(path))
            spans = (
                scan.macros.get(macro, ())
                if macro is not None
                else scan.templates.get(templates or "", ())
            )
            found.extend(Reference(path, start, end) for start, end in spans)
        return found

    def _scan(self, path: Path, text: str | None) -> _Scan:
        """Return where a file calls macros and names templates.

        A saved file is read once for each time it changes. A file the editor
        holds is read from its text, which may not be saved.
        """
        if text is None:
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                return _Scan(0, {}, {})
            cached = self._scans.get(path)
            if cached is not None and cached.stamp == stamp:
                return cached
            scan = self._scanned(path, _read(path), stamp)
            self._scans[path] = scan
            return scan
        return self._scanned(path, text, 0)

    def _scanned(self, path: Path, text: str, stamp: int) -> _Scan:
        macros: dict[str, list[tuple[int, int]]] = {}
        templates: dict[str, list[tuple[int, int]]] = {}
        if path.suffix == ".py":
            for match in _PYTHON_CALL.finditer(text):
                macros.setdefault(match.group(1).lower(), []).append(match.span(1))
            for start, end, name in _template_names(text):
                templates.setdefault(name, []).append((start, end))
        else:
            for match in self._name_at.finditer(text):
                macros.setdefault(match.group(2).lower(), []).append(match.span(2))
            for match in self._include_path.finditer(text):
                templates.setdefault(match.group(1), []).append(match.span(1))
        return _Scan(stamp, macros, templates)

    def _project_files(self) -> tuple[list[Path], list[Path]]:
        """Every template and file of macros, and every Python file of the project.

        The paths are resolved, as the editor's open files are keyed.
        """
        if self._files is None:
            roots = [Path(root).resolve() for root in self.project.templates.paths]
            templates = (
                next((root / name for root in roots if (root / name).is_file()), None)
                for name in self.names()
            )
            sql_files = [
                *(path for path in templates if path is not None),
                *(path.resolve() for path in self.project.macro_files()),
                *sorted(self._macro_sql),
            ]
            root = self.project.root.resolve()
            python_files = [
                path
                for path in sorted(root.rglob("*.py"))
                if not any(
                    part in SKIPPED for part in path.relative_to(root).parts[:-1]
                )
            ]
            self._files = (sql_files, python_files)
        return self._files

    def _macro_at(self, source: str, offset: int) -> str | None:
        start = source.rfind("\n", 0, offset) + 1
        end = source.find("\n", offset)
        line = source[start : len(source) if end < 0 else end]
        for match in self._name_at.finditer(line):
            if match.start() <= offset - start <= match.end():
                return match.group(2).lower()
        return None

    def _macros(self, typed: str) -> list[Completion]:
        namespace = self.project.templates.namespace
        found = [
            Completion(
                macro.name,
                "macro",
                signature_of(macro, namespace),
                macro.doc,
                _snippet(macro),
            )
            for macro in self.project.templates.macros.values()
            if macro.name.startswith(typed)
        ]
        if INCLUDE.startswith(typed):
            found.append(
                Completion(
                    INCLUDE,
                    "macro",
                    f"{namespace}.{INCLUDE}('path.sql')",
                    "The query of another template, in parentheses.",
                    f"{INCLUDE}('${{1}}')",
                )
            )
        return found

    def _templates(self, typed: str) -> list[Completion]:
        return [
            Completion(name, "template")
            for name in self.project.templates.names()
            if name.startswith(typed)
        ]

    @staticmethod
    def _parameters(source: str) -> list[Completion]:
        names = dict.fromkeys(match.group(1) for match in _PARAMETERS.finditer(source))
        return [Completion(name, "parameter") for name in names]

    def _placed(self, error: SQLAKitError, name: str, source: str) -> Diagnostic:
        """Return the error where it is in this text.

        A problem in a template this one includes is put on the line of the
        `include` that leads to it.
        """
        chain = getattr(error, "chain", ())
        if chain and chain[0][0] == name:
            start, end = _line_span(source, chain[0][1])
            return Diagnostic(start, end, str(error))
        span = getattr(error, "span", None)
        if span is None:
            span = _line_span(source, getattr(error, "line", 1))
        message = getattr(error, "problem", "") or str(error)
        if isinstance(error, MacroArgumentError):
            message = f"{self.project.templates.namespace}.{error.name}: {message}"
        return Diagnostic(span[0], span[1], message)


_TEMPLATE_CALLS = {"sql", "from_file", "from_sql"}
"""The calls whose first argument names a template: `db.sql(...)` and its kin."""

_TEMPLATE_CALL_NAMED = re.compile(rf"\b(?:{'|'.join(_TEMPLATE_CALLS)})\b")
"""The name of a call that reads a template, anywhere in the text: a file without
one is not parsed."""

_TEMPLATE_TYPED = re.compile(r"\.(?:sql|from_file|from_sql)\(\s*[\"']([^\"']*)$")

_ARGUMENT_TYPED = re.compile(
    r"""\.(?:sql|from_file|from_sql)\(\s*["'](?P<name>[^"']+\.sql)["']\s*,"""
    r"(?P<passed>[^()]*?)(?:^|[\s,])(?P<typed>\w*)$",
    re.MULTILINE,
)
"""A call that names a template, and the keyword being typed after its name."""

_PASSED = re.compile(r"(\w+)\s*=")


@dataclass(frozen=True, slots=True)
class _TemplateCall:
    """A call that reads a template, and the values it passes by name."""

    name: str
    keywords: tuple[tuple[str, int, int], ...]
    """Each keyword's name, and where it is written."""
    open: bool
    """Whether it passes values the code does not name: `**values` or a context."""


def _template_calls(source: str) -> list[_TemplateCall]:
    """Return each call of `.sql(...)`, `.from_file(...)` or `.from_sql(...)`."""
    if not _TEMPLATE_CALL_NAMED.search(source):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    found = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _TEMPLATE_CALLS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.endswith(".sql")
        ):
            continue
        keywords = []
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg == "context":
                continue
            start = _char_offset(source, starts, keyword.lineno, keyword.col_offset)
            keywords.append((keyword.arg, start, start + len(keyword.arg)))
        found.append(
            _TemplateCall(
                node.args[0].value,
                tuple(keywords),
                len(node.args) > 1
                or any(keyword.arg in (None, "context") for keyword in node.keywords),
            )
        )
    return found


def _template_names(source: str) -> list[tuple[int, int, str]]:
    """Return where the code names a template, the quotes left out, and the name.

    A name is the first argument of `.sql(...)`, `.sql.from_file(...)` or
    `.from_sql(...)`, written as a string that ends in `.sql`.
    """
    if not _TEMPLATE_CALL_NAMED.search(source):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    found = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _TEMPLATE_CALLS
            and node.args
        ):
            continue
        name = node.args[0]
        if not (
            isinstance(name, ast.Constant)
            and isinstance(name.value, str)
            and name.value.endswith(".sql")
            and name.end_lineno is not None
            and name.end_col_offset is not None
        ):
            continue
        start = _char_offset(source, starts, name.lineno, name.col_offset)
        end = _char_offset(source, starts, name.end_lineno, name.end_col_offset)
        quote = source.find(name.value, start, end)
        if quote >= 0:
            found.append((quote, quote + len(name.value), name.value))
    return found


def _char_offset(source: str, starts: list[int], line: int, column: int) -> int:
    """Return the offset of a position `ast` gives, its column in UTF-8 bytes."""
    begin = starts[line - 1]
    end = starts[line] if line < len(starts) else len(source)
    return begin + len(source[begin:end].encode()[:column].decode(errors="ignore"))


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _line_span(source: str, line: int) -> tuple[int, int]:
    """Return the offsets of a line, counting lines from one."""
    start = 0
    for _ in range(line - 1):
        newline = source.find("\n", start)
        if newline < 0:
            break
        start = newline + 1
    end = source.find("\n", start)
    return start, len(source) if end < 0 else end


def _snippet(macro: Macro) -> str:
    """Return a call to the macro with a placeholder for each required argument."""
    placeholders = [
        f":${{{index}:{slot.name}}}"
        if slot.kind is Param
        else f"${{{index}:{slot.name}}}"
        for index, slot in enumerate((slot for slot in macro.slots if slot.required), 1)
    ]
    return f"{macro.name}({', '.join(placeholders)})"


def _source_of(macro: Macro) -> Target | None:
    """Return where a macro's name is written: in its `SELECT`, or its `def`."""
    written = getattr(macro, "path", None)
    named = getattr(macro, "name_at", None)
    if isinstance(written, Path) and named is not None:
        return Target(written, named[0] - 1, named[1])
    try:
        path = inspect.getsourcefile(macro.func)
        lines, first = inspect.getsourcelines(macro.func)
    except (OSError, TypeError):
        return None
    if path is None:
        return None
    for index, line in enumerate(lines):
        if found := _DEF.match(line):
            return Target(Path(path), first - 1 + index, found.start(1))
    return Target(Path(path), max(first - 1, 0))


_DEF = re.compile(r"\s*(?:async\s+)?def\s+(\w+)")


@dataclass(frozen=True, slots=True)
class _At:
    """The text a request is about, the offset in it, and whether it is Python."""

    helper: _Assistant
    source: str
    offset: int
    python: bool = False


def offset_of(source: str, line: int, character: int) -> int:
    """Return the offset of a position the protocol gives, in UTF-16 units."""
    start = 0
    for _ in range(line):
        newline = source.find("\n", start)
        if newline < 0:
            return len(source)
        start = newline + 1
    end = source.find("\n", start)
    text = source[start : len(source) if end < 0 else end]
    units = 0
    for index, char in enumerate(text):
        if units >= character:
            return start + index
        units += 2 if ord(char) > 0xFFFF else 1  # noqa: PLR2004 - past the BMP
    return start + len(text)


def position_of(source: str, offset: int) -> tuple[int, int]:
    """Return the line and the UTF-16 column of an offset, both from zero."""
    line = source.count("\n", 0, offset)
    start = source.rfind("\n", 0, offset) + 1
    column = len(source[start:offset].encode("utf-16-le")) // 2
    return line, column


def serve() -> None:  # pragma: no cover - run over stdio by an editor
    """Run the language server over standard input and output."""
    _server().start_io()


def _server() -> Any:  # noqa: ANN401, C901, PLR0915 - a handler for each request
    server = LanguageServer("sqlakit", version("sqlakit-lsp"))
    state: dict[str, Any] = {"assistant": None, "problem": None}

    def assistant() -> _Assistant | None:
        return state["assistant"]

    def load(ls: LanguageServer) -> None:
        """Read the project, and say in the editor's log what was found where."""
        try:
            project = load_project(Path(ls.workspace.root_path or "."))
        except ProjectConfigError as error:
            state["problem"] = str(error)
            ls.window_show_message(
                types.ShowMessageParams(types.MessageType.Warning, str(error))
            )
            return
        helper = _Assistant(project)
        if (previous := assistant()) is not None:
            helper.keep(previous)
        state["assistant"] = helper
        ls.window_log_message(
            types.LogMessageParams(types.MessageType.Info, "\n".join(project.found))
        )

    @server.feature(types.INITIALIZED)
    async def initialized(ls: LanguageServer, _: Any) -> None:  # noqa: ANN401
        load(ls)
        # Files made or removed outside the editor change which templates there
        # are, so the server asks to hear of them, where the editor can say.
        ls.client_register_capability(
            types.RegistrationParams(
                [
                    types.Registration(
                        "sqlakit-files",
                        types.WORKSPACE_DID_CHANGE_WATCHED_FILES,
                        types.DidChangeWatchedFilesRegistrationOptions(
                            [
                                types.FileSystemWatcher("**/*.sql"),
                                types.FileSystemWatcher("**/*.py"),
                            ]
                        ),
                    )
                ]
            )
        )
        # The first references read every file: read them now, a file at a time,
        # and answer what the editor asks between them.
        helper = assistant()
        if helper is not None:
            for _ in helper.scan_all():
                await asyncio.sleep(0)
                if assistant() is not helper:
                    break

    def publish(ls: LanguageServer, uri: str) -> None:
        document = ls.workspace.get_text_document(uri)
        path = _path(uri)
        helper = assistant()
        found: list[Diagnostic] = []
        if helper is not None and helper.reads_python(path):
            found = helper.python_diagnose(document.source)
        elif helper is not None and helper.applies_to(path):
            found = helper.diagnose(path, document.source)
        ls.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(
                uri=uri,
                version=document.version,
                diagnostics=[_diagnostic(document.source, one) for one in found],
            )
        )

    @server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    def did_open(ls: LanguageServer, params: Any) -> None:  # noqa: ANN401
        publish(ls, params.text_document.uri)

    @server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
    def did_change(ls: LanguageServer, params: Any) -> None:  # noqa: ANN401
        publish(ls, params.text_document.uri)

    @server.feature(types.TEXT_DOCUMENT_DID_SAVE)
    def did_save(ls: LanguageServer, params: Any) -> None:  # noqa: ANN401
        path = _path(params.text_document.uri)
        # An editor that watches no files says a new one is there by saving it.
        helper = assistant()
        changed(ls, [path], moved=helper is not None and not helper.lists(path))
        publish(ls, params.text_document.uri)

    @server.feature(types.WORKSPACE_DID_CHANGE_WATCHED_FILES)
    def watched(ls: LanguageServer, params: Any) -> None:  # noqa: ANN401
        moved = any(
            change.type != types.FileChangeType.Changed for change in params.changes
        )
        changed(ls, [_path(change.uri) for change in params.changes], moved=moved)

    def changed(ls: LanguageServer, paths: list[Path], *, moved: bool) -> None:
        """Take in files that changed on disk.

        Python and files of macros can add or change a macro, so the project is
        read again. A file made or removed changes which files there are.
        """
        helper = assistant()
        if helper is None:
            return
        if any(path.suffix == ".py" or helper.holds_macros(path) for path in paths):
            load(ls)
        if moved and (helper := assistant()) is not None:
            helper.forget_files()

    def at(ls: LanguageServer, params: Any) -> _At | None:  # noqa: ANN401
        helper = assistant()
        path = _path(params.text_document.uri)
        if helper is None:
            return None
        python = helper.reads_python(path) or (
            path.suffix == ".py" and helper.defines_macros(path)
        )
        if not (python or helper.applies_to(path)):
            return None
        source = ls.workspace.get_text_document(params.text_document.uri).source
        position = params.position
        offset = offset_of(source, position.line, position.character)
        return _At(helper, source, offset, python=python)

    kinds = {
        "macro": types.CompletionItemKind.Function,
        "template": types.CompletionItemKind.File,
        "parameter": types.CompletionItemKind.Variable,
    }

    @server.feature(
        types.TEXT_DOCUMENT_COMPLETION,
        types.CompletionOptions(trigger_characters=[".", "'", '"', ":"]),
    )
    def completion(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        found = at(ls, params)
        if found is None:
            return None
        helper, source, offset = found.helper, found.source, found.offset
        written = (
            helper.python_complete(source, offset)
            if found.python
            else helper.complete(source, offset)
        )
        return types.CompletionList(
            is_incomplete=False,
            items=[
                types.CompletionItem(
                    label=one.label,
                    kind=kinds[one.kind],
                    detail=one.detail or None,
                    documentation=types.MarkupContent(
                        types.MarkupKind.Markdown, one.documentation
                    )
                    if one.documentation
                    else None,
                    insert_text=one.snippet,
                    insert_text_format=types.InsertTextFormat.Snippet
                    if one.snippet
                    else None,
                )
                for one in written
            ],
        )

    @server.feature(
        types.TEXT_DOCUMENT_SIGNATURE_HELP,
        types.SignatureHelpOptions(trigger_characters=["(", ","]),
    )
    def signature_help(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        found = at(ls, params)
        signature = (
            None
            if found is None or found.python
            else found.helper.signature(found.source, found.offset)
        )
        if signature is None:
            return None
        return types.SignatureHelp(
            signatures=[
                types.SignatureInformation(
                    label=signature.label,
                    documentation=types.MarkupContent(
                        types.MarkupKind.Markdown, signature.doc
                    ),
                    parameters=[
                        types.ParameterInformation(label=name)
                        for name in signature.arguments
                    ],
                )
            ],
            active_signature=0,
            active_parameter=signature.active,
        )

    @server.feature(types.TEXT_DOCUMENT_HOVER)
    def hover(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        found = at(ls, params)
        text = (
            None
            if found is None or found.python
            else found.helper.hover(found.source, found.offset)
        )
        if text is None:
            return None
        return types.Hover(types.MarkupContent(types.MarkupKind.Markdown, text))

    def located(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        """Return where the macro or the template under the cursor is written."""
        found = at(ls, params)
        if found is None:
            return None
        path = _path(params.text_document.uri)
        target = (
            found.helper.python_definition(found.source, found.offset, path)
            if found.python
            else found.helper.definition(found.source, found.offset, path)
        )
        if target is None:
            return None
        start = types.Position(target.line, _utf16_column(target))
        uri, place = target.path.resolve().as_uri(), types.Range(start, start)
        span = found.helper.origin(
            found.source, found.offset, python=found.python, path=path
        )
        if span is None or not links_supported(ls):
            return types.Location(uri, place)
        # A link carries the span it starts from, which the editor underlines.
        origin = types.Range(
            types.Position(*position_of(found.source, span[0])),
            types.Position(*position_of(found.source, span[1])),
        )
        return [types.LocationLink(uri, place, place, origin)]

    def links_supported(ls: LanguageServer) -> bool:
        """Whether the editor takes a definition as a link with the span it starts from."""
        document = ls.client_capabilities.text_document
        definition = None if document is None else document.definition
        return bool(definition is not None and definition.link_support)

    for method in (types.TEXT_DOCUMENT_DEFINITION, types.TEXT_DOCUMENT_DECLARATION):
        server.feature(method)(located)

    @server.feature(types.TEXT_DOCUMENT_IMPLEMENTATION)
    def implemented(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        """Return the function of a macro from its SQL file, and back.

        Anything else has one place, which `located` gives.
        """
        found = at(ls, params)
        if found is None:
            return None
        path = _path(params.text_document.uri)
        target = found.helper.implementation(path, found.source, found.offset)
        if target is None:
            return located(ls, params)
        start = types.Position(target.line, _utf16_column(target))
        return types.Location(target.path.resolve().as_uri(), types.Range(start, start))

    @server.feature(types.TEXT_DOCUMENT_REFERENCES)
    def references(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        """Return every place that names the macro or the template under the cursor."""
        found = at(ls, params)
        if found is None:
            return None
        helper, source, offset = found.helper, found.source, found.offset
        path = _path(params.text_document.uri)
        texts = {
            _path(uri).resolve(): document.source
            for uri, document in ls.workspace.text_documents.items()
        }

        def read(path: Path) -> str:
            """Return a file's text, as the editor holds it when it is open."""
            resolved = path.resolve()
            if resolved not in texts:
                texts[resolved] = _read(path)
            return texts[resolved]

        places = helper.references(path, source, offset, texts)
        name = helper.macro_named(path, source, offset)
        if params.context.include_declaration and name is not None:
            declared = helper.declaration(name)
            places = places if declared is None else [declared, *places]
        return [
            types.Location(
                place.path.resolve().as_uri(),
                types.Range(
                    types.Position(*position_of(read(place.path), place.start)),
                    types.Position(*position_of(read(place.path), place.end)),
                ),
            )
            for place in places
        ]

    def held(ls: LanguageServer) -> dict[Path, str]:
        """Return the text of every file the editor has open, by its path."""
        return {
            _path(uri).resolve(): document.source
            for uri, document in ls.workspace.text_documents.items()
        }

    @server.feature(types.TEXT_DOCUMENT_PREPARE_RENAME)
    def prepare_rename(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        found = at(ls, params)
        if found is None:
            return None
        path = _path(params.text_document.uri)
        span = found.helper.renamable(path, found.source, found.offset)
        if isinstance(span, str):
            raise RequestFailed(span)
        return types.Range(
            types.Position(*position_of(found.source, span[0])),
            types.Position(*position_of(found.source, span[1])),
        )

    @server.feature(types.TEXT_DOCUMENT_RENAME)
    def rename(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        found = at(ls, params)
        if found is None:
            return None
        texts = held(ls)
        path = _path(params.text_document.uri)
        renamed = found.helper.rename(
            path, found.source, found.offset, params.new_name, texts
        )
        if isinstance(renamed, str):
            raise RequestFailed(renamed)
        places, moved = renamed
        by_file: dict[Path, list[Reference]] = {}
        for place in places:
            by_file.setdefault(place.path.resolve(), []).append(place)

        def text_of(path: Path) -> str:
            return texts[path] if path in texts else _read(path)

        changes: list[Any] = [
            types.TextDocumentEdit(
                types.OptionalVersionedTextDocumentIdentifier(file.as_uri(), None),
                [
                    types.TextEdit(
                        types.Range(
                            types.Position(*position_of(text_of(file), one.start)),
                            types.Position(*position_of(text_of(file), one.end)),
                        ),
                        params.new_name,
                    )
                    for one in edits
                ],
            )
            for file, edits in by_file.items()
        ]
        if moved is not None:
            old, new = moved
            changes.append(
                types.RenameFile(old.resolve().as_uri(), new.resolve().as_uri())
            )
        return types.WorkspaceEdit(document_changes=changes)

    @server.feature(types.TEXT_DOCUMENT_DOCUMENT_LINK)
    def document_link(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        helper = assistant()
        path = _path(params.text_document.uri)
        if helper is None or not (helper.reads_python(path) or helper.applies_to(path)):
            return []
        source = ls.workspace.get_text_document(params.text_document.uri).source
        return [
            types.DocumentLink(
                range=types.Range(
                    types.Position(*position_of(source, start)),
                    types.Position(*position_of(source, end)),
                ),
                target=target.resolve().as_uri(),
            )
            for start, end, target in helper.links(path, source)
        ]

    return server


def _utf16_column(target: Target) -> int:
    """Return a target's column in the UTF-16 units the protocol counts in."""
    if not target.column:
        return 0
    try:
        line = target.path.read_text(encoding="utf-8").splitlines()[target.line]
    except (OSError, IndexError):
        return target.column
    return len(line[: target.column].encode("utf-16-le")) // 2


def _diagnostic(source: str, found: Diagnostic) -> types.Diagnostic:
    start = types.Position(*position_of(source, found.start))
    end = types.Position(*position_of(source, max(found.end, found.start)))
    return types.Diagnostic(
        range=types.Range(start, end),
        message=found.message,
        severity=types.DiagnosticSeverity.Error,
        source="sqlakit",
    )


def _path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))
