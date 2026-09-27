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
import bisect
import difflib
import inspect
import json
import os
import re
import subprocess
import tempfile
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from functools import cached_property, lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlakit
import sqlalchemy as sa
import sqlalchemy.engine.default
import sqlalchemy.exc
from lsprotocol import types
from pygls.exceptions import JsonRpcException
from pygls.lsp.server import LanguageServer
from pygls.protocol import LanguageServerProtocol
from pygls.protocol.language_server import lsp_method
from pygls.uris import to_fs_path
from sqlakit._project import Project, load_project
from sqlakit._sql import (
    _LITERAL,
    INCLUDE,
    NAMESPACE,
    Context,
    Macro,
    MacroTemplate,
    Param,
    SqlMacro,
    calls_kept,
    signature_of,
    sql_macros,
)
from sqlakit._static import SKIPPED, StaticMacro, walk
from sqlakit.exceptions import (
    MacroArgumentError,
    MacroSyntaxError,
    ProjectConfigError,
    SQLAKitError,
    UnknownMacroError,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Mapping, Sequence

__all__ = ["Completion", "Diagnostic", "Target", "serve"]

_PARAMETERS = re.compile(r"(?<![:\w\\]):([A-Za-z_]\w*)(?:\.\w+)*")
"""A `:parameter`, with the path read off it, `:criteria.teams`: the group is its
name, and the match all of it."""


class _Example:
    """A value to render a template with: any path reads another of it.

    `:criteria.teams` and `:filters["kind"]` both read one, so a template
    renders whatever shape its values have. It writes `x`, and holds one value.
    """

    def __getattr__(self, _: str) -> _Example:
        return self

    def __getitem__(self, _: object) -> _Example:
        return self

    def __iter__(self) -> Iterator[_Example]:
        return iter((self,))

    def __len__(self) -> int:
        return 1

    def __str__(self) -> str:
        return "x"


_PARAMETER_TYPED = re.compile(r"(?<![:\w\\]):\w*$")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A problem in the text, from one offset to another."""

    start: int
    end: int
    message: str
    severity: str = "error"
    """`error`, `warning`, or `hint` for what is likely fine."""


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
    replaces: int = 0
    """How much of the text before the cursor it replaces: the part of a path
    typed, which an editor's word would stop at a `/` in."""


@dataclass(frozen=True, slots=True)
class Target:
    """A definition's place: a file, and the line and column in it, from zero.

    The column counts characters, and the server gives it to the editor in the
    UTF-16 units the protocol counts in.
    """

    path: Path
    line: int
    column: int = 0
    length: int = 0
    """How many characters of the line the place spans: a name to select."""


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


_BOUND = re.compile(
    rf"{_LITERAL}|(?<![:\w\\]):(?P<bound>[A-Za-z_]\w*)(?![\w:])", re.DOTALL
)
"""A placeholder of rendered SQL, and the strings and comments to skip."""

_LIVE = Path(__file__).with_name("_live.py")
"""The script the project's interpreter renders a template with."""

_LIVE_SECONDS = 20
"""How long a render with the project's macros may take: it imports them."""


def _interpreter(root: Path) -> Path | None:
    """Return the Python of the project's `.venv`, if it has one."""
    for found in (
        root / ".venv" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
    ):
        if found.is_file():
            return found
    return None


def _run_live(root: Path, request: dict[str, Any]) -> tuple[str | None, str]:
    """Render a template in the project's interpreter: the SQL, or why not."""
    python = _interpreter(root)
    if python is None:
        return None, "the project has no .venv to run its macros in."
    try:
        done = subprocess.run(  # noqa: S603 - the project's own interpreter
            [str(python), str(_LIVE)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            cwd=root,
            timeout=_LIVE_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"running {_relative(python, root)} failed: {error}"
    try:
        answer = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        last = (done.stderr.strip().splitlines() or ["no output"])[-1]
        return None, f"{_relative(python, root)} failed: {last}"
    if "sql" in answer:
        return answer["sql"], ""
    return None, str(answer.get("error", "no SQL came back"))


def _calls(parts: Sequence[Any]) -> Iterator[Any]:
    """Yield every macro call of a read template, the ones in arguments too."""
    for part in parts:
        if isinstance(part, str):
            continue
        yield part
        for argument in part.args:
            yield from _calls(argument.parts)


def _rendered(template: Any, dialect: str, values: dict[str, Any]) -> str | None:  # noqa: ANN401
    """Return the SQL a read template writes for these values, if it can.

    A call that cannot be made with them, such as one of a macro of the
    project's own Python, which is read and not run, stays a call.
    """
    try:
        with calls_kept():
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
class Symbol:
    """A name a file holds, for its outline: a macro, a template or a parameter."""

    name: str
    kind: str
    start: int
    end: int
    detail: str = ""


def _word_span(
    source: str, word: str, at: tuple[int, int] | None
) -> tuple[int, int] | None:
    """Return where a name stands in the text: on its line when it is known."""
    if at is not None:
        start = _line_span(source, at[0])[0] + at[1]
        if source[start : start + len(word)].lower() == word:
            return start, start + len(word)
    found = re.search(rf"\b{re.escape(word)}\b", source, re.IGNORECASE)
    return None if found is None else found.span()


@dataclass(frozen=True, slots=True)
class Fix:
    """A name to write in place of a span of text, and what the editor calls it."""

    title: str
    start: int
    end: int
    text: str


_NOT_READ = re.compile(r"`\w+` is not a parameter of `(?P<template>[^`]+)`")


def _similar(
    written: str,
    names: Sequence[str],
    start: int,
    end: int,
    namespace: str | None = None,
) -> list[Fix]:
    """Return a fix for each name close to the one written, the closest first."""
    shown = f"{namespace}." if namespace else ""
    return [
        Fix(f"Write `{shown}{name}`", start, end, name)
        for name in difflib.get_close_matches(written, names, n=3, cutoff=0.6)
    ]


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


@dataclass(frozen=True, slots=True)
class Passing:
    """A call of Python that passes a template's parameter."""

    path: Path
    line: int
    start: int
    end: int
    """Where the value is: its keyword, or the code that passes values the call
    does not name, `values` in `**values`."""
    code: str
    """What the call writes, `page_size=limit` or `**values`."""
    named: bool = True
    """Whether the call names the parameter, and does not only may pass it."""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class _Scan:
    """Where a file calls each macro and names each template, by name."""

    stamp: int
    """The file's modification time when it was read, or 0 for unsaved text."""
    macros: dict[str, list[tuple[int, int]]]
    templates: dict[str, list[tuple[int, int]]]
    calls: dict[str, list[_TemplateCall]] = field(default_factory=dict)
    """The calls of Python that read each template, with what they pass."""


_PYTHON_CALL = re.compile(r"(?<![\w.])tpl\.(\w+)")
"""A macro called from Python, through the `tpl` object: `tpl.icontains(...)`."""

_WORD = re.compile(r"\w+")

SHOW = "Show rendered SQL"
"""The title of the action that shows a whole template as SQL."""

SHOW_MARKED = "Show rendered SQL with ?"
"""The title of the action that shows it with `?` for each parameter."""

RENDER = "sqlakit.render"
"""The command that writes a whole template as SQL, for an editor to open."""

_RENDERED = Path(tempfile.gettempdir()) / "sqlakit-rendered"
"""Where rendered templates are written: outside the project, as they are
nothing to keep."""

_LISTED = 10
"""The most calls a hover lists by name, the rest counted."""

_TOKEN_TYPES = {"namespace": 0, "function": 1, "parameter": 2}
"""What an editor colours, by the index the legend of the server gives it."""

_SYMBOLS = 200
"""The most names a search of the project answers with: the editor narrows."""

_COMPILES = 64
"""How many texts of templates stay read: the ones the editor has open, mostly."""


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
        self._marked = re.compile(
            rf"{_LITERAL}"
            rf"|(?<![\w.])(?P<namespace>{namespace})\.(?P<macro>[A-Za-z_]\w*)(?=\s*\()"
            r"|(?<![:\w\\]):(?P<param>[A-Za-z_]\w*(?:\.\w+)*)",
            re.IGNORECASE | re.DOTALL,
        )
        """A macro's call or a parameter, past the strings and comments."""
        self._macro_sql = {
            Path(sql).resolve()
            for macro in project.templates.macros.values()
            if (sql := getattr(macro, "sql_path", None)) is not None
        }
        """The files `@sql_macro("file.sql")` keeps its SQL in."""
        self._macro_files = {path.resolve() for path in project.macro_files()} | {
            macro.path.resolve()
            for macro in project.templates.macros.values()
            if isinstance(macro, SqlMacro)
        }
        """The files of SQL macros, those that register none yet among them."""
        self._root = project.root.resolve()
        self._template_roots = [
            Path(root).resolve() for root in project.templates.paths
        ]
        self._sources: set[Path] | None = None
        self._passed: dict[str, frozenset[str] | None] = {}
        self._including: dict[str, set[str]] | None = None
        self._everything: list[tuple[str, str, Target, str]] | None = None
        self._compiles: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._names: list[str] | None = None
        self._files: tuple[list[Path], list[Path]] | None = None
        self._named: dict[Path, str] = {}
        """The name of each template in the list of files."""
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
            self._named = previous._named
            self._including = previous._including

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

    def _source(self, macro: Macro) -> Target | None:
        """Return where a macro is written, in the project's own `sqlakit`.

        A server that `uvx` runs imports `sqlakit` from a cache of its own. The
        project holds the same version in `.venv`, which is the file to open.
        """
        target = _source_of(macro)
        if target is None or self._library is None:
            return target
        try:
            inside = target.path.resolve().relative_to(_LIBRARY)
        except ValueError:
            return target
        return replace(target, path=self._library / inside)

    @cached_property
    def _library(self) -> Path | None:
        """Return the project's `sqlakit` in `.venv`, when it is this version."""
        installed = f"sqlakit-{version('sqlakit')}.dist-info"
        for site in (
            *self._root.glob(".venv/lib/python*/site-packages"),
            self._root / ".venv/Lib/site-packages",
        ):
            library = site / "sqlakit"
            if (site / installed).is_dir() and library.resolve() != _LIBRARY:
                return library
        return None

    def owns(self, path: Path) -> bool:
        """Whether a file is the project's own, where `walk` goes.

        A virtual environment, a cache or a hidden directory is not.
        """
        try:
            parts = path.resolve().relative_to(self._root).parts[:-1]
        except ValueError:
            return False
        return not any(part in SKIPPED or part.startswith(".") for part in parts)

    def is_template(self, path: Path) -> bool:
        """Whether a file is under a template directory, saved or not."""
        resolved = path.resolve()
        return path.suffix == ".sql" and any(
            resolved.is_relative_to(root) for root in self._template_roots
        )

    def forget_files(self) -> None:
        """Read the list of templates and Python files again on the next request."""
        self._names = self._files = self._everything = self._including = None
        self._passed.clear()

    def names(self) -> list[str]:
        """Every template's name, read once until `forget_files`."""
        if self._names is None:
            self._names = sorted(self._on_disk())
        return self._names

    def _on_disk(self) -> dict[str, Path]:
        """Return each template's name and file, as `Templates.names` finds them.

        One walk of the directories, in strings: a file made has the next
        keystroke read them again, and paths cost more than the walk.
        """
        skipped = {str(path) for path in self._macro_files | self._macro_sql}
        found: dict[str, Path] = {}
        for root in self._template_roots:
            start = len(str(root)) + 1
            for directory, _, files in os.walk(root):
                for file in files:
                    written = os.path.join(directory, file)  # noqa: PTH118 - strings, for speed
                    if file.endswith(".sql") and written not in skipped:
                        name = written[start:].replace(os.sep, "/")
                        found.setdefault(name, Path(written))
        return found

    def defines_macros(self, path: Path) -> bool:
        """Whether a file defines a macro the project calls, its own or built in.

        The library's own file is one: references on `def identifier` there are
        the calls of `tpl.identifier` in the templates.
        """
        if self._sources is None:
            self._sources = {
                target.path.resolve()
                for macro in self.project.templates.macros.values()
                if (target := self._source(macro)) is not None
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
            self.compiled(name, source)
        except (MacroSyntaxError, UnknownMacroError, MacroArgumentError) as error:
            return [self._placed(error, name, source)]
        found = [Diagnostic(*one) for one in self.project.foreign_calls(source)]
        if (template := self._last_compiled(name, source)) is not None:
            found.extend(self.unpassed(name, source, template))
        return found

    def _last_compiled(self, name: str, source: str) -> MacroTemplate | None:
        """Return the template `diagnose` has just read, from the cache."""
        found = self._compiles.get((name, source))
        return found if isinstance(found, MacroTemplate) else None

    def unpassed(
        self, name: str, source: str, template: MacroTemplate
    ) -> list[Diagnostic]:
        """Return each parameter of a template that no call of the project passes.

        One close to a name the calls do pass is likely a typo, and a warning.
        Any other is a hint: a template may offer what no call uses yet. Nothing
        is said when no call reads the template, or one passes values it does
        not name, `**values` or a context.
        """
        passed = self.passed(name)
        if passed is None:
            return []
        listed = ", ".join(f"`{one}`" for one in sorted(passed)) or "nothing"
        found = []
        for param in sorted(template.parameters() - passed):
            written = re.search(rf"(?<![:\w\\]):{re.escape(param)}\b", source)
            if written is None:
                continue
            if difflib.get_close_matches(param, passed, n=1, cutoff=0.6):
                message = f"No call passes `:{param}`, and the calls pass {listed}."
                found.append(Diagnostic(*written.span(), message, "warning"))
            else:
                message = f"No call in the project's Python passes `:{param}`."
                found.append(Diagnostic(*written.span(), message, "hint"))
        return found

    def passed(self, name: str) -> frozenset[str] | None:
        """Return every name the calls that read a template pass, or None.

        The calls are those of Python that read it, and those that read a
        template that includes it, however deep. None when there is no call,
        or one passes values it does not name. Kept until files change.
        """
        if name in self._passed:
            return self._passed[name]
        calls = [call for _, call in self._calls_reading(name)]
        passed = (
            None
            if not calls or any(call.open for call in calls)
            else frozenset(keyword.name for call in calls for keyword in call.keywords)
        )
        self._passed[name] = passed
        return passed

    def _calls_reading(self, name: str) -> list[tuple[Path, _TemplateCall]]:
        """Return the calls of Python that read a template, and their files.

        A call that reads a template that includes it, however deep, reads it
        too.
        """
        included_by = self._included_by()
        readers, waiting = {name}, [name]
        while waiting:
            for including in included_by.get(waiting.pop(), ()):
                if including not in readers:
                    readers.add(including)
                    waiting.append(including)
        return [
            (path, call)
            for path in self._project_files()[1]
            for reader in sorted(readers)
            for call in self._scan(path, None).calls.get(reader, ())
        ]

    def _included_by(self) -> dict[str, set[str]]:
        """Return, for each template, the templates that include it."""
        if self._including is None:
            including: dict[str, set[str]] = {}
            self._project_files()
            for path, reader in self._named.items():
                for included in self._scan(path, None).templates:
                    including.setdefault(included, set()).add(reader)
            self._including = including
        return self._including

    def compiled(self, name: str, source: str) -> MacroTemplate:
        """Return a template read from its text, reading each text once.

        Diagnostics, hover and the parameters of a call all ask for the text the
        editor holds, often the same one. A template read before is read again
        when a template it includes has changed on disk.

        Raises:
            SQLAKitError: whatever reading the template raises, raised again.

        """
        key = (name, source)
        found = self._compiles.get(key)
        if isinstance(found, MacroTemplate) and not self._includes_changed(found):
            self._compiles.move_to_end(key)
            return found
        if isinstance(found, SQLAKitError):
            self._compiles.move_to_end(key)
            raise found
        try:
            found = self.project.load(name, source)
        except SQLAKitError as error:
            # An error in an included file goes when that file is fixed, which
            # the text in hand does not show: only this text's own is kept.
            if not getattr(error, "chain", ()):
                self._keep(key, error)
            raise
        self._keep(key, found)
        return found

    def _keep(self, key: tuple[str, str], found: MacroTemplate | SQLAKitError) -> None:
        self._compiles[key] = found
        while len(self._compiles) > _COMPILES:
            self._compiles.popitem(last=False)

    def _includes_changed(self, template: MacroTemplate) -> bool:
        for included, mtime in template.includes.items():
            path = self.project.path_of(included)
            if path is None or path.stat().st_mtime != mtime:
                return True
        return False

    def tokens(self, source: str) -> list[tuple[int, int, str, bool]]:
        """Return the spans an editor colours, with their kind, and if built in.

        They are the namespace and the name of a known macro's call, and each
        parameter. A macro no one registered is left alone, so a typo stands out.
        """
        macros = self.project.templates.macros
        found = []
        for match in self._marked.finditer(source):
            if match.group("param"):
                found.append((*match.span(), "parameter", False))
                continue
            name = (match.group("macro") or "").lower()
            if name != INCLUDE and name not in macros:
                continue
            own = isinstance(macros.get(name), StaticMacro | SqlMacro)
            found.append((*match.span("namespace"), "namespace", not own))
            found.append((*match.span("macro"), "function", not own))
        return found

    def symbols(self, path: Path, source: str) -> list[Symbol]:
        """Return what a file holds, for an editor's outline of it.

        A file of macros holds its macros. A template holds the templates it
        includes, the macros it calls and its parameters, each once, where it
        first stands.
        """
        if self.holds_macros(path):
            return self._macro_symbols(path, source)
        namespace = self.project.templates.namespace
        found: list[Symbol] = []
        seen: set[str] = set()

        def once(key: str, symbol: Symbol) -> None:
            if key not in seen:
                seen.add(key)
                found.append(symbol)

        for match in self._include_path.finditer(source):
            once(
                f"include {match.group(1)}",
                Symbol(match.group(1), "template", *match.span(1)),
            )
        for match in self._name_at.finditer(source):
            name = match.group(2).lower()
            if name != INCLUDE:
                once(
                    f"macro {name}",
                    Symbol(f"{namespace}.{name}", "macro", *match.span()),
                )
        for match in _PARAMETERS.finditer(source):
            once(
                f"param {match.group(1)}",
                Symbol(f":{match.group(1)}", "parameter", *match.span()),
            )
        return found

    def _macro_symbols(self, path: Path, source: str) -> list[Symbol]:
        """Return each macro a file of macros defines, where its name stands."""
        resolved = path.resolve()
        namespace = self.project.templates.namespace
        found = []
        for macro in self.project.templates.macros.values():
            written = getattr(macro, "sql_path", None) or getattr(macro, "path", None)
            if written is None or Path(written).resolve() != resolved:
                continue
            span = _word_span(source, macro.name, getattr(macro, "name_at", None))
            if span is not None:
                detail = signature_of(macro, namespace)
                found.append(Symbol(macro.name, "macro", *span, detail))
        return found

    def workspace_symbols(self, query: str) -> list[tuple[str, str, Target, str]]:
        """Return the macros and the templates whose names hold the query."""
        if self._everything is None:
            namespace = self.project.templates.namespace
            everything = [
                (
                    f"{namespace}.{macro.name}",
                    "macro",
                    target,
                    signature_of(macro, namespace),
                )
                for macro in self.project.templates.macros.values()
                if (target := self._source(macro)) is not None
            ]
            everything.extend(
                (name, "template", Target(path, 0), "")
                for name in self.names()
                if (path := self.project.path_of(name)) is not None
            )
            self._everything = everything
        wanted = query.lower()
        return [one for one in self._everything if wanted in one[0].lower()][:_SYMBOLS]

    def fixes(self, source: str, start: int, end: int, message: str) -> list[Fix]:
        """Return what could replace the text of a problem this server reported.

        A call under `tpl` when the namespace is another, an unknown macro, an
        unknown template and a value a template does not read each have the
        names they could have meant.
        """
        namespace = self.project.templates.namespace
        text = source[start:end]
        if "is not a macro call: the namespace is" in message:
            prefix = start + len(NAMESPACE) + 1
            called = source[prefix:end]
            return [
                Fix(f"Write `{namespace}.{called}`", start, prefix, f"{namespace}.")
            ]
        if message.lower().startswith("unknown macro"):
            found = self._name_at.search(source, start, end)
            if found is None:
                return []
            names = [*self.project.templates.macros, INCLUDE]
            return _similar(found.group(2), names, *found.span(2), namespace)
        return self._names_fixes(text, start, end, message)

    def _names_fixes(self, text: str, start: int, end: int, message: str) -> list[Fix]:
        """Return the names close to a template's or a parameter's written."""
        if message.startswith("No SQL template named"):
            return _similar(text, self.names(), start, end)
        if message.startswith("No call passes `:"):
            passed = re.findall(r"`(\w+)`", message.split("the calls pass", 1)[1])
            return [
                Fix(f"Write `:{name}`", start, end, f":{name}")
                for name in difflib.get_close_matches(text[1:], passed, n=3, cutoff=0.6)
            ]
        if (named := _NOT_READ.match(message)) is not None:
            reads = self.parameters(named.group("template")) or frozenset()
            return _similar(text, sorted(reads), start, end)
        return []

    def rendered(self, path: Path, source: str) -> str:
        """Return the whole template as the database gets it, on the project's dialect.

        Every parameter counts as given, so every optional part is there, and
        stays a placeholder. A parameter a built-in macro cannot take made up,
        the column `tpl.order_by` sorts by, counts as not given.

        The server reads the project's Python macros and does not run them, so
        when the template calls one, it is rendered again by the project's own
        interpreter, `.venv/bin/python`, which imports them. Without one, or
        when that fails, the calls stay calls, and a comment on top says why.
        """
        name = self.project.name_of(path) or path.name
        dialect = self.project.dialect or "postgresql"
        try:
            template = self.compiled(name, source)
        except SQLAKitError as error:
            return f"-- {name} cannot be read: {error}\n"
        calls = list(_calls(template.parts))
        values: dict[str, Any] = dict.fromkeys(template.parameters(), _Example())
        values.update(dict.fromkeys(self._not_given(name, source, calls, dialect)))
        sql = _rendered(template, dialect, values)
        if sql is None:
            return f"-- {name} cannot be written with made-up values.\n"
        namespace = self.project.templates.namespace
        # A call of an included template is in the SQL too, and not in `calls`.
        kept = {
            key: macro
            for key, macro in self.project.templates.macros.items()
            if isinstance(macro, StaticMacro)
            and re.search(rf"(?<![\w.]){namespace}\.{key}\s*\(", sql, re.IGNORECASE)
        }
        if not kept:
            return f"-- {name} on {dialect}\n{sql}\n"
        request = {
            "root": str(self.project.root),
            "name": name,
            "source": source,
            "dialect": dialect,
            "modules": sorted({str(macro.path) for macro in kept.values()}),
            "macros": sorted(kept),
            "not_given": sorted(key for key, value in values.items() if value is None),
        }
        run, problem = _run_live(self.project.root, request)
        if run is not None:
            return f"-- {name} on {dialect}\n{run}\n"
        listed = ", ".join(f"{namespace}.{key}" for key in sorted(kept))
        return f"-- {name} on {dialect}\n-- {listed} stays a call: {problem}\n{sql}\n"

    def rendered_marked(self, path: Path, source: str) -> str:
        """Return the rendered template with `?` for each parameter, in order.

        A comment on top lists what each `?` stands for, as a driver that
        takes `?` is given the values in that order.
        """
        text = self.rendered(path, source)
        names: list[str] = []

        def marked(found: re.Match[str]) -> str:
            name = found.group("bound")
            if name is None:
                return found.group(0)
            names.append(name)
            return "?"

        text = _BOUND.sub(marked, text)
        if not names:
            return text
        first, _, rest = text.partition("\n")
        listed = ", ".join(f":{name}" for name in names)
        return f"{first}\n-- ? in order: {listed}\n{rest}"

    def _not_given(
        self, name: str, source: str, calls: Sequence[Any], dialect: str
    ) -> set[str]:
        """Return the parameters to render as not given.

        They are those of a call of a built-in macro that cannot be made with a
        made-up value, and can with none: `:sort` of `tpl.order_by`, whose value
        must be a column it lists.
        """
        found: set[str] = set()
        for call in calls:
            if isinstance(self.project.templates.macros.get(call.name), StaticMacro):
                continue
            text = source[call.span[0] : call.span[1]]
            try:
                alone = self.compiled(name, text)
            except SQLAKitError:
                continue
            names = alone.parameters()
            given = _rendered(alone, dialect, dict.fromkeys(names, _Example()))
            if names and given in (None, text.strip()):
                missing = _rendered(alone, dialect, dict.fromkeys(names))
                if missing not in (None, text.strip()):
                    found.update(names)
        return found

    def holds_macros(self, path: Path) -> bool:
        """Whether a file defines macros: SQL macros, or the SQL of a file macro."""
        return self._reads_macros(path) or path.resolve() in self._macro_sql

    def _reads_macros(self, path: Path) -> bool:
        return path.resolve() in self._macro_files

    def _diagnose_macros(self, path: Path, source: str) -> list[Diagnostic]:
        """Return what is wrong with each macro of a file of SQL macros."""
        try:
            problems = self.project.macro_problems(path, source)
        except MacroSyntaxError as error:
            # Half typed, the file cannot be split into macros at all.
            return [self._placed(error, path.name, source)]
        return [
            *(
                Diagnostic(*_line_span(source, line), message)
                for line, message in problems
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

    def hover(self, source: str, offset: int, path: Path | None = None) -> str | None:
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
        template = (
            "<hover>" if path is None else self.project.name_of(path) or path.name
        )
        written = self.written(source, offset, template)
        if not written:
            return f"```sql\n{signature}\n```\n\n{macro.doc}".rstrip()
        return f"{written}\n\n```sql\n{signature}\n```\n\n{macro.doc}".rstrip()

    def written(self, source: str, offset: int, name: str = "<hover>") -> str:
        """Return the SQL the call under the offset writes, as Markdown.

        It is written on the project's dialect: once with every parameter it
        reads given, and once with none, when the two differ. Empty when the call
        cannot be written without real values.
        """
        try:
            template = self.compiled(name, source)
        except SQLAKitError:
            return ""
        call = _innermost(template.parts, offset)
        # A macro of the project's own Python would write only its call again.
        if call is None or isinstance(
            self.project.templates.macros.get(call.name), StaticMacro
        ):
            return ""
        text = source[call.span[0] : call.span[1]]
        try:
            alone = self.compiled(name, text)
        except SQLAKitError:
            return ""
        names = sorted(alone.parameters())
        dialect = self.project.dialect or "postgresql"
        given = _rendered(alone, dialect, dict.fromkeys(names, _Example()))
        missing = _rendered(alone, dialect, dict.fromkeys(names))
        # A call that cannot be made writes itself, which says nothing new.
        given, missing = (
            None if one == text.strip() else one for one in (given, missing)
        )
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

    def passes(
        self, name: str, param: str
    ) -> tuple[list[Passing], list[tuple[Path, int]]]:
        """Return the calls of Python that pass a template's parameter, and not.

        A call that does not pass it is a file and a line. A call that passes
        values it does not name, `**values` or a context, counts as one that
        may pass it. The calls of a template that includes this one count too.
        """
        passing, silent = [], []
        for path, call in self._calls_reading(name):
            keyword = next((one for one in call.keywords if one.name == param), None)
            if keyword is not None:
                code = f"{param}={keyword.value}"
                passing.append(
                    Passing(path, keyword.line, keyword.start, keyword.end, code)
                )
            elif call.open:
                passing.extend(
                    Passing(path, one.line, one.start, one.end, one.value, named=False)
                    for one in call.unnamed
                )
            else:
                silent.append((path, call.line))
        return passing, silent

    def _parameter_at(
        self, path: Path, source: str, offset: int
    ) -> tuple[str, re.Match[str]] | None:
        """Return the template and the `:parameter` under the offset."""
        name = self.project.name_of(path)
        if name is None:
            return None
        found = next(
            (
                match
                for match in _PARAMETERS.finditer(source)
                if match.start() <= offset <= match.end()
            ),
            None,
        )
        return None if found is None else (name, found)

    def parameter_hover(self, path: Path, source: str, offset: int) -> str | None:
        """Return which calls pass the parameter under the offset, what, and which not."""
        at = self._parameter_at(path, source, offset)
        if at is None:
            return None
        name, found = at
        param = found.group(1)
        passing, silent = self.passes(name, param)
        root = self.project.root

        def listed(places: Sequence[tuple[Path, int, str | None]]) -> str:
            shown = [
                f"- `{_relative(path, root)}:{line}`"
                + ("" if code is None else f" `{code}`")
                for path, line, code in places[:_LISTED]
            ]
            if len(places) > _LISTED:
                shown.append(f"- and {len(places) - _LISTED} more")
            return "\n".join(shown)

        parts = [f"`:{param}` of `{name}`"]
        if passing:
            given = [(one.path, one.line, one.code) for one in passing]
            parts.append(f"Passed by {len(passing)}:\n\n{listed(given)}")
        if silent:
            missing = [(path, line, None) for path, line in silent]
            parts.append(f"Not passed by {len(silent)}:\n\n{listed(missing)}")
        if not passing and not silent:
            parts.append("No call in the project's Python reads this template.")
        return "\n\n".join(parts)

    def parameter_definitions(
        self, path: Path, source: str, offset: int
    ) -> list[Target]:
        """Return where the Python passes the parameter under the offset.

        That is the keyword of each call that names it. Only when none does, it
        is the code of the calls that pass values they do not name: `values` in
        `**values`, or a context that is not written out.
        """
        at = self._parameter_at(path, source, offset)
        if at is None:
            return []
        name, found = at
        passing = self.passes(name, found.group(1))[0]
        named = [one for one in passing if one.named]
        texts: dict[Path, str] = {}
        targets = []
        for one in named or passing:
            text = texts.get(one.path)
            if text is None:
                text = texts[one.path] = _read(one.path)
            line = text.count("\n", 0, one.start)
            column = one.start - (text.rfind("\n", 0, one.start) + 1)
            targets.append(Target(one.path, line, column, one.end - one.start))
        return targets

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
            return self._source(macro)
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
            return self._source(macro)
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
                    for start, end, _ in self._files_named(source, path)
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
        for match in _PARAMETERS.finditer(source, start, end):
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
                    keyword.start,
                    keyword.end,
                    f"`{keyword.name}` is not a parameter of `{call.name}`, "
                    f"which reads {listed}.",
                )
                for keyword in call.keywords
                if keyword.name not in reads
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
            reads = self.compiled(name, _read(path)).parameters()
        except SQLAKitError:
            return None
        self._reads[name] = (stamp, reads)
        return reads

    def python_complete(self, source: str, offset: int) -> list[Completion]:
        """Return the template names that can go where the code reads one."""
        typed = source[source.rfind("\n", 0, offset) + 1 : offset]
        if match := _TEMPLATE_TYPED.search(typed):
            return [
                Completion(name, "template", replaces=len(match.group(1)))
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
        """Return the file the name under the offset reads.

        That is a template in `db.sql("...")`, or the SQL of a macro in
        `@sql_macro("...")`. On the name of a `def` that defines a macro, it is
        the name itself.
        """
        for start, end, found in self._files_named(source, path):
            if start <= offset <= end:
                return Target(found, 0)
        return None if path is None else self._itself(path, source, offset)

    def _files_named(
        self, source: str, path: Path | None
    ) -> list[tuple[int, int, Path]]:
        """Return each file the Python names, where the name is, and the file.

        A template is looked for under the template directories, and the SQL of
        `@sql_macro("...")` next to the module, as `sql_macro` reads it.
        """
        found = [
            (start, end, target)
            for start, end, name in _template_names(source)
            if (target := self.project.path_of(name)) is not None
        ]
        if path is not None:
            found.extend(
                (start, end, target)
                for start, end, name in _sql_macro_files(source)
                if (target := path.parent / name).is_file()
            )
        return found

    def links(self, path: Path, source: str) -> list[tuple[int, int, Path]]:
        """Return each file the text names, where the name is, and the file."""
        if path.suffix == ".py":
            return self._files_named(source, path)
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
        # On `tpl.mine`, the name renames, whichever half the cursor is on.
        span = self._called_at(source, offset) or _word_at(source, offset)
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
        if macro is None or (target := self._source(macro)) is None:
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
        target = self._source(macro)
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
            calls: dict[str, list[_TemplateCall]] = {}
            for call in _template_calls(text):
                calls.setdefault(call.name, []).append(call)
                if call.span is not None:
                    templates.setdefault(call.name, []).append(call.span)
            return _Scan(stamp, macros, templates, calls)
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
            templates = self._on_disk()
            self._names = sorted(templates)
            named = {path: name for name, path in templates.items()}
            sql_files = [
                *named,
                *(path.resolve() for path in self.project.macro_files()),
                *sorted(self._macro_sql),
            ]
            python_files = [
                directory / file
                for directory, files in walk(self._root)
                for file in files
                if file.endswith(".py")
            ]
            self._named = named
            self._files = (sql_files, python_files)
        return self._files

    def _macro_at(self, source: str, offset: int) -> str | None:
        span = self._called_at(source, offset)
        return None if span is None else source[slice(*span)].lower()

    def _called_at(self, source: str, offset: int) -> tuple[int, int] | None:
        """Return where the name of the macro called at the offset is written."""
        start = source.rfind("\n", 0, offset) + 1
        end = source.find("\n", offset)
        line = source[start : len(source) if end < 0 else end]
        for match in self._name_at.finditer(line):
            if match.start() <= offset - start <= match.end():
                return start + match.start(2), start + match.end(2)
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
            Completion(name, "template", replaces=len(typed))
            for name in self.names()
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
    r"(?P<passed>[^()]*?)(?<=[\s,])(?P<typed>\w*)\Z",
)
"""A call that names a template, and the keyword being typed after its name."""

_PASSED = re.compile(r"(\w+)\s*=")


_SHOWN = 60
"""The most characters of a value's code a hover shows."""


@dataclass(frozen=True, slots=True)
class _Keyword:
    """A value a call passes by name, where the name is written, and its code."""

    name: str
    start: int
    end: int
    value: str
    line: int


@dataclass(frozen=True, slots=True)
class _TemplateCall:
    """A call that reads a template, and the values it passes by name."""

    name: str
    keywords: tuple[_Keyword, ...]
    """Each keyword, and where it is written."""
    open: bool
    """Whether it passes values the code does not name: `**values` or a context."""
    line: int = 1
    span: tuple[int, int] | None = None
    """Where the template's name is written, without its quotes."""
    unnamed: tuple[_Keyword, ...] = ()
    """The code that passes values it does not name: where `values` of
    `**values` is, and all of it written, or a context's."""


def _unnamed(node: ast.expr, written: str, source: str, starts: list[int]) -> _Keyword:
    """Return code that passes values it does not name, as a keyword with no name."""
    start = _char_offset(source, starts, node.lineno, node.col_offset)
    end = _char_offset(
        source,
        starts,
        node.end_lineno or node.lineno,
        node.end_col_offset or node.col_offset,
    )
    code = _code(node, source, starts)
    # The name the editor selects: the value, or the start of a long one.
    end = end if source.count("\n", start, end) == 0 else source.find("\n", start)
    return _Keyword("", start, end, written.format(code), node.lineno)


def _code(node: ast.expr, source: str, starts: list[int]) -> str:
    """Return the code of an expression on one line, cut short when it is long."""
    start = _char_offset(source, starts, node.lineno, node.col_offset)
    end = _char_offset(
        source,
        starts,
        node.end_lineno or node.lineno,
        node.end_col_offset or node.col_offset,
    )
    text = " ".join(source[start:end].split())
    return text if len(text) <= _SHOWN else f"{text[: _SHOWN - 1]}…"


def _named_keys(
    node: ast.expr, source: str, starts: list[int]
) -> list[_Keyword] | None:
    """Return the keys of a dict written out, and where each is, or None.

    None is a context the code does not write out: a name, a call, or a dict
    with `**` in it.
    """
    if not isinstance(node, ast.Dict) or any(key is None for key in node.keys):
        return None
    found = []
    for key, value in zip(node.keys, node.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        start = _char_offset(source, starts, key.lineno, key.col_offset) + 1
        found.append(
            _Keyword(
                key.value,
                start,
                start + len(key.value),
                _code(value, source, starts),
                key.lineno,
            )
        )
    return found


_LINE_BREAK = re.compile(r"\r\n?|\n")
"""A line break as Python reads one: not a form feed, nor a Unicode separator."""


def _line_starts(source: str) -> list[int]:
    """Return where each line of Python starts, as `ast` numbers the lines."""
    return [0, *(found.end() for found in _LINE_BREAK.finditer(source))]


def _template_calls(source: str) -> list[_TemplateCall]:
    """Return each call of `.sql(...)`, `.from_file(...)` or `.from_sql(...)`."""
    if not _TEMPLATE_CALL_NAMED.search(source):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    starts = _line_starts(source)
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
            keywords.append(
                _Keyword(
                    keyword.arg,
                    start,
                    start + len(keyword.arg),
                    _code(keyword.value, source, starts),
                    keyword.lineno,
                )
            )
        # A context written out as a dict names its values as keywords do.
        contexts = [
            *node.args[1:2],
            *(keyword.value for keyword in node.keywords if keyword.arg == "context"),
        ]
        written = [_named_keys(context, source, starts) for context in contexts]
        for keys in written:
            keywords.extend(keys or ())
        unnamed = [
            *(
                _unnamed(context, "{}", source, starts)
                for context, keys in zip(node.args[1:2], written, strict=False)
                if keys is None
            ),
            *(
                _unnamed(keyword.value, "context={}", source, starts)
                for keyword, keys in zip(
                    [one for one in node.keywords if one.arg == "context"],
                    written[len(node.args[1:2]) :],
                    strict=True,
                )
                if keys is None
            ),
            *(_unnamed(one, "{}", source, starts) for one in node.args[2:]),
            *(
                _unnamed(keyword.value, "**{}", source, starts)
                for keyword in node.keywords
                if keyword.arg is None
            ),
        ]
        named = node.args[0]
        value = str(named.value) if isinstance(named, ast.Constant) else ""
        span = None
        if named.end_lineno is not None and named.end_col_offset is not None:
            start = _char_offset(source, starts, named.lineno, named.col_offset)
            end = _char_offset(source, starts, named.end_lineno, named.end_col_offset)
            quote = source.find(value, start, end)
            span = None if quote < 0 else (quote, quote + len(value))
        found.append(
            _TemplateCall(
                value, tuple(keywords), bool(unnamed), node.lineno, span, tuple(unnamed)
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
    starts = _line_starts(source)
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


def _sql_macro_files(source: str) -> list[tuple[int, int, str]]:
    """Return where `@sql_macro("file.sql")` names its SQL, the quotes left out."""
    if "sql_macro" not in source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    starts = _line_starts(source)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call) and decorator.args):
                continue
            func = decorator.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            name = decorator.args[0]
            if not (
                called == "sql_macro"
                and isinstance(name, ast.Constant)
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

_LIBRARY = Path(sqlakit.__file__).resolve().parent
"""The `sqlakit` this server imports."""


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
    starts = _starts(source)
    line = bisect.bisect_right(starts, offset) - 1
    text = source[starts[line] : offset]
    column = len(text) if text.isascii() else len(text.encode("utf-16-le")) // 2
    return line, column


@lru_cache(maxsize=8)
def _starts(source: str) -> list[int]:
    """Return where each line starts, for the few texts a request reads.

    A string keeps its hash, and a request asks of the same one each time, so
    the lookup costs nothing next to counting the lines again.
    """
    return [0, *(found.end() for found in re.finditer("\n", source))]


class _Protocol(LanguageServerProtocol):
    """The protocol, with positions counted in UTF-16 whatever the editor offers.

    Every offset here is counted the way UTF-16 counts, the one encoding each
    editor takes, so the server announces no other.
    """

    @lsp_method(types.INITIALIZE)
    def lsp_initialize(
        self, params: types.InitializeParams
    ) -> Generator[Any, Any, types.InitializeResult]:
        if params.capabilities.general is not None:
            params.capabilities.general.position_encodings = None
        return (yield from super().lsp_initialize(params))


def serve() -> None:  # pragma: no cover - run over stdio by an editor
    """Run the language server over standard input and output."""
    _server().start_io()


def _server() -> Any:  # noqa: ANN401, C901, PLR0915 - a handler for each request
    server = LanguageServer("sqlakit", version("sqlakit-lsp"), protocol_cls=_Protocol)
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
                                types.FileSystemWatcher("**/pyproject.toml"),
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

    @server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
    def did_close(ls: LanguageServer, params: Any) -> None:  # noqa: ANN401
        # What a closed file holds is no longer in the editor's list of problems.
        ls.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(uri=params.text_document.uri, diagnostics=[])
        )

    @server.feature(types.TEXT_DOCUMENT_DID_SAVE)
    def did_save(ls: LanguageServer, params: Any) -> None:  # noqa: ANN401
        path = _path(params.text_document.uri)
        # An editor that watches no files says a new one is there by saving it.
        helper = assistant()
        if not changed(ls, [path], moved=helper is not None and not helper.lists(path)):
            publish(ls, params.text_document.uri)

    @server.feature(types.WORKSPACE_DID_CHANGE_WATCHED_FILES)
    def watched(ls: LanguageServer, params: Any) -> None:  # noqa: ANN401
        moved = any(
            change.type != types.FileChangeType.Changed for change in params.changes
        )
        changed(ls, [_path(change.uri) for change in params.changes], moved=moved)

    def changed(ls: LanguageServer, paths: list[Path], *, moved: bool) -> bool:
        """Take in files that changed on disk, and say if the open ones were checked.

        A file that configures the project, builds `Templates(...)` or defines
        a macro, has the project read again. A template or a Python file made or
        removed changes which files there are. A file of a virtual environment,
        a cache or anything else is left alone. The open files are checked again
        after either, since what they call may have changed.
        """
        helper = assistant()
        if helper is None:
            # The project could not be read: a change may be what fixes it.
            if not any(
                path.suffix in {".sql", ".py"} or path.name == "pyproject.toml"
                for path in paths
            ):
                return False
            load(ls)
        else:
            own = [
                path for path in paths if helper.owns(path) or helper.holds_macros(path)
            ]
            reload = any(
                helper.holds_macros(path)
                or helper.defines_macros(path)
                or _may_configure(path)
                for path in own
            )
            moved = moved and any(
                helper.is_template(path) or path.suffix == ".py" for path in own
            )
            if not (reload or moved):
                return False
            if reload:
                load(ls)
            if moved and (helper := assistant()) is not None:
                helper.forget_files()
        for uri in list(ls.workspace.text_documents):
            publish(ls, uri)
        return True

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
                    text_edit=types.TextEdit(
                        types.Range(
                            types.Position(*position_of(source, offset - one.replaces)),
                            types.Position(*position_of(source, offset)),
                        ),
                        one.label,
                    )
                    if one.replaces
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
            else found.helper.parameter_hover(
                _path(params.text_document.uri), found.source, found.offset
            )
            or found.helper.hover(
                found.source, found.offset, _path(params.text_document.uri)
            )
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
        if found.python:
            target = found.helper.python_definition(found.source, found.offset, path)
        else:
            target = found.helper.definition(found.source, found.offset, path)
        # A parameter is defined where the Python passes it, by each call.
        targets = (
            [target]
            if target is not None
            else []
            if found.python
            else found.helper.parameter_definitions(path, found.source, found.offset)
        )
        if not targets:
            return None
        places = []
        for one in targets:
            start, end = _utf16_columns(one)
            place = types.Range(
                types.Position(one.line, start), types.Position(one.line, end)
            )
            places.append((one.path.resolve().as_uri(), place))
        span = found.helper.origin(
            found.source, found.offset, python=found.python, path=path
        )
        if span is None or not links_supported(ls):
            if len(places) == 1:
                return types.Location(*places[0])
            return [types.Location(uri, place) for uri, place in places]
        # A link carries the span it starts from, which the editor underlines.
        origin = types.Range(
            types.Position(*position_of(found.source, span[0])),
            types.Position(*position_of(found.source, span[1])),
        )
        return [types.LocationLink(uri, place, place, origin) for uri, place in places]

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
            if not renames_files(ls):
                problem = "Renaming a template renames its file, and this editor renames none."
                raise RequestFailed(problem)
            old, new = moved
            changes.append(
                types.RenameFile(old.resolve().as_uri(), new.resolve().as_uri())
            )
        return types.WorkspaceEdit(document_changes=changes)

    def renames_files(ls: LanguageServer) -> bool:
        """Whether the editor renames a file as part of an edit."""
        workspace = ls.client_capabilities.workspace
        edit = None if workspace is None else workspace.workspace_edit
        operations = None if edit is None else edit.resource_operations
        return bool(operations and types.ResourceOperationKind.Rename in operations)

    def shows_documents(ls: LanguageServer) -> bool:
        """Whether the editor opens a file the server asks it to."""
        window = ls.client_capabilities.window
        shown = None if window is None else window.show_document
        return bool(shown is not None and shown.support)

    def resolves_edits(ls: LanguageServer) -> bool:
        """Whether the editor asks for an action's edit only once it is chosen."""
        document = ls.client_capabilities.text_document
        action = None if document is None else document.code_action
        support = None if action is None else action.resolve_support
        return bool(support is not None and "edit" in support.properties)

    def rendered_file(
        ls: LanguageServer, uri: str, *, marked: bool = False
    ) -> tuple[Path, str]:
        """Return a new file for the rendered template, and what goes in it."""
        helper = assistant()
        path = _path(uri)
        source = ls.workspace.get_text_document(uri).source
        name = helper.project.name_of(path) or path.name if helper else path.name
        if helper is None:
            text = ""
        elif marked:
            text = helper.rendered_marked(path, source)
        else:
            text = helper.rendered(path, source)
        # A new file each time: the editor may still hold the one before.
        written = _RENDERED / uuid.uuid4().hex[:8] / name
        written.parent.mkdir(parents=True, exist_ok=True)
        return written, text

    def rendered_edit(
        ls: LanguageServer, uri: str, *, marked: bool = False
    ) -> types.WorkspaceEdit:
        """Return an edit that makes the file of the rendered template."""
        written, text = rendered_file(ls, uri, marked=marked)
        target = written.as_uri()
        start = types.Position(0, 0)
        return types.WorkspaceEdit(
            document_changes=[
                types.CreateFile(target),
                types.TextDocumentEdit(
                    types.OptionalVersionedTextDocumentIdentifier(target, None),
                    [types.TextEdit(types.Range(start, start), text)],
                ),
            ]
        )

    # An editor asks for an action's edit only from a server that says it gives one.
    @server.feature(
        types.TEXT_DOCUMENT_CODE_ACTION, types.CodeActionOptions(resolve_provider=True)
    )
    def code_actions(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        """Offer the whole template, rendered, in a template.

        An editor that opens a file the server asks it to gets a command that
        does. Any other gets an edit that makes the file, which it opens as it
        applies the edit, worked out only once chosen where the editor allows.
        """
        helper = assistant()
        path = _path(params.text_document.uri)
        if helper is None:
            return None
        uri = params.text_document.uri
        actions = fixes(ls, params)
        if helper.project.name_of(path) is None or helper.holds_macros(path):
            return actions or None
        if not path.name.endswith(".sql"):
            return actions or None
        for title, marked in ((SHOW, False), (SHOW_MARKED, True)):
            action = types.CodeAction(title=title, kind=types.CodeActionKind.Empty)
            if shows_documents(ls):
                action.command = types.Command(
                    title=title, command=RENDER, arguments=[uri, marked]
                )
            elif resolves_edits(ls):
                action.data = {"uri": uri, "marked": marked}
            else:
                action.edit = rendered_edit(ls, uri, marked=marked)
            actions.append(action)
        return actions

    def fixes(ls: LanguageServer, params: Any) -> list[types.CodeAction]:  # noqa: ANN401
        """Return a quick fix for each problem of ours the editor sends along.

        Only the problems at the cursor come with the request, so nothing is
        looked for where there is nothing wrong.
        """
        helper = assistant()
        ours = [one for one in params.context.diagnostics if one.source == "sqlakit"]
        if helper is None or not ours:
            return []
        uri = params.text_document.uri
        source = ls.workspace.get_text_document(uri).source
        found = []
        for problem in ours:
            start = offset_of(
                source, problem.range.start.line, problem.range.start.character
            )
            end = offset_of(source, problem.range.end.line, problem.range.end.character)
            for fix in helper.fixes(source, start, end, problem.message):
                edit = types.TextEdit(
                    types.Range(
                        types.Position(*position_of(source, fix.start)),
                        types.Position(*position_of(source, fix.end)),
                    ),
                    fix.text,
                )
                found.append(
                    types.CodeAction(
                        title=fix.title,
                        kind=types.CodeActionKind.QuickFix,
                        diagnostics=[problem],
                        edit=types.WorkspaceEdit(changes={uri: [edit]}),
                    )
                )
        return found

    @server.feature(types.CODE_ACTION_RESOLVE)
    def resolve_action(ls: LanguageServer, action: types.CodeAction) -> Any:  # noqa: ANN401
        """Work out the edit of an action once the editor has chosen it."""
        data = action.data or {}
        if "uri" in data:
            action.edit = rendered_edit(
                ls, data["uri"], marked=bool(data.get("marked"))
            )
        return action

    @server.command(RENDER)
    async def render(ls: LanguageServer, uri: str, marked: bool = False) -> None:  # noqa: FBT001, FBT002 - a command's arguments come in order
        """Write the rendered template to a file, and have the editor open it."""
        written, text = rendered_file(ls, uri, marked=marked)
        written.write_text(text, encoding="utf-8")
        await ls.window_show_document_async(
            types.ShowDocumentParams(written.as_uri(), take_focus=True)
        )

    @server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
    def configured(_ls: LanguageServer, _params: Any) -> None:  # noqa: ANN401
        """Take the settings an editor sends: the server has none."""

    symbol_kinds = {
        "macro": types.SymbolKind.Function,
        "template": types.SymbolKind.File,
        "parameter": types.SymbolKind.Variable,
    }

    @server.feature(types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
    def document_symbols(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        helper = assistant()
        path = _path(params.text_document.uri)
        if helper is None or not helper.applies_to(path):
            return None
        source = ls.workspace.get_text_document(params.text_document.uri).source
        found = []
        for one in helper.symbols(path, source):
            place = types.Range(
                types.Position(*position_of(source, one.start)),
                types.Position(*position_of(source, one.end)),
            )
            found.append(
                types.DocumentSymbol(
                    name=one.name,
                    kind=symbol_kinds[one.kind],
                    range=place,
                    selection_range=place,
                    detail=one.detail or None,
                )
            )
        return found

    @server.feature(types.WORKSPACE_SYMBOL)
    def workspace_symbols(_ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        helper = assistant()
        if helper is None:
            return None
        found = []
        for name, kind, target, detail in helper.workspace_symbols(params.query):
            start = types.Position(target.line, _utf16_column(target))
            found.append(
                types.SymbolInformation(
                    name=name,
                    kind=symbol_kinds[kind],
                    location=types.Location(
                        target.path.resolve().as_uri(), types.Range(start, start)
                    ),
                    container_name=detail or None,
                )
            )
        return found

    @server.feature(
        types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
        types.SemanticTokensLegend(
            token_types=list(_TOKEN_TYPES), token_modifiers=["defaultLibrary"]
        ),
    )
    def semantic_tokens(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        """Colour the macros' calls and the parameters of a template."""
        helper = assistant()
        path = _path(params.text_document.uri)
        if helper is None or not helper.applies_to(path):
            return None
        source = ls.workspace.get_text_document(params.text_document.uri).source
        data: list[int] = []
        line = column = 0
        for start, end, kind, built_in in helper.tokens(source):
            at_line, at_column = position_of(source, start)
            length = position_of(source, end)[1] - at_column
            data.extend(
                (
                    at_line - line,
                    at_column - column if at_line == line else at_column,
                    length,
                    _TOKEN_TYPES[kind],
                    int(built_in),
                )
            )
            line, column = at_line, at_column
        return types.SemanticTokens(data=data)

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
    return _utf16_columns(target)[0]


def _utf16_columns(target: Target) -> tuple[int, int]:
    """Return where a target starts and ends on its line, in UTF-16 units."""
    end = target.column + target.length
    if not end:
        return 0, 0
    try:
        line = _lines(target.path, target.path.stat().st_mtime_ns)[target.line]
    except (OSError, IndexError):
        return target.column, end
    if line.isascii():
        return target.column, end
    units = [
        len(line[:column].encode("utf-16-le")) // 2 for column in (target.column, end)
    ]
    return units[0], units[1]


@lru_cache(maxsize=64)
def _lines(path: Path, _stamp: int) -> list[str]:
    """Return a file's lines, read once for each time it changes."""
    return _LINE_BREAK.split(path.read_text(encoding="utf-8"))


_SEVERITIES = {
    "error": types.DiagnosticSeverity.Error,
    "warning": types.DiagnosticSeverity.Warning,
    "hint": types.DiagnosticSeverity.Hint,
}


def _diagnostic(source: str, found: Diagnostic) -> types.Diagnostic:
    start = types.Position(*position_of(source, found.start))
    end = types.Position(*position_of(source, max(found.end, found.start)))
    return types.Diagnostic(
        range=types.Range(start, end),
        message=found.message,
        severity=_SEVERITIES[found.severity],
        source="sqlakit",
    )


_CONFIGURES = ("sql_macro", "Templates", "Database", "templates")
"""What a Python file that says where the templates or the macros are holds."""


def _may_configure(path: Path) -> bool:
    """Whether a changed file may change the project: where templates are, or a macro.

    A Python file removed may have been one, and is taken as one.
    """
    if path.name == "pyproject.toml":
        return True
    if path.suffix != ".py":
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return any(word in text for word in _CONFIGURES)


def _path(uri: str) -> Path:
    """Return the file of a `file:` URI, a drive and all on Windows."""
    return Path(to_fs_path(uri) or uri)
