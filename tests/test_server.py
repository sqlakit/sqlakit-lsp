"""The server: what an editor is told about a project's templates."""

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from sqlakit._project import load_project
from sqlakit._sql import sql_macros

from sqlakit_lsp._server import (
    RENDER,
    Completion,
    Diagnostic,
    Fix,
    Reference,
    Target,
    _Assistant,
    _source_of,
    _utf16_column,
    offset_of,
    position_of,
)

PYPROJECT = """
[project]
name = "app"
"""

DB = """
import os
from pathlib import Path

from sqlakit import Database
from sqlakit.sql import Templates

HERE = Path(__file__).parent

db = Database(
    os.environ["DATABASE_URL"],
    templates=Templates(HERE / "sql", macros=[HERE / "_macros.sql"]),
)
"""

MACROS = '''
from sqlakit.sql import Param, sql_macro


@sql_macro
def mine(teams: Param) -> str:
    """Rows of any of the teams."""
    return f"team IN {teams}"
'''

SQL_MACROS = """-- Rows of the team the call asks for.
SELECT t.team = :team AS for_team FROM t;

SELECT tpl.for_team(t) OR t.public AS visible FROM t;
"""

TEMPLATES = {
    "good.sql": "SELECT * FROM users\nWHERE tpl.mine(:teams)\n  AND tpl.if_set(:q, name = :q)",
    "inner.sql": "SELECT 1\nWHERE tpl.nope(:x)",
    "outer.sql": "SELECT *\nFROM tpl.include('inner.sql') AS i",
    "open.sql": "SELECT 1,\n  'never closed",
}


APP = {
    "pyproject.toml": "[project]\nname = 'shop'\n",
    "shop/__init__.py": "",
    "shop/db.py": """
from pathlib import Path

from sqlakit import Database
from sqlakit.sql import Templates

BASE_DIR = Path(__file__).parent / "sql"

db = Database(
    "sqlite://",
    templates=Templates(
        BASE_DIR,
        macros=["shop.macros", BASE_DIR / "_macros.sql"],
        namespace="q",
    ),
)
raise RuntimeError("imported")
""",
    "shop/macros.py": '''
from typing import Literal

from sqlakit.sql import Context, Param, Sql, sql_macro


@sql_macro(optional=True)
def owned(ctx: Context, team: Param, *columns: Sql) -> str:
    """Rows of the team."""
    raise RuntimeError("called")


@sql_macro(name="sided")
def side(which: Literal["'left'", "'right'"] = "'left'") -> str:
    return which
''',
    "shop/sql/_macros.sql": "-- Rows of the team.\nSELECT t.team = :team AS for_team FROM t;\n",
    "shop/sql/users.sql": "SELECT * FROM users AS u WHERE q.owned(:team, u.a) AND q.for_team(u)\n",
    "tests/test_it.py": "from sqlakit.sql import Templates\nTemplates('elsewhere')\n",
}


CODE = """from app import User, db, other

print("имя"); db.sql("good.sql", teams=[])
db.sql.from_file("missing.sql")
User.query.from_sql("inner.sql")
db.sql.from_string("SELECT 1 -- not a file.sql")
other.sql("not_a_template")
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "db.py").write_text(DB)
    (tmp_path / "lsp_macros.py").write_text(MACROS)
    (tmp_path / "_macros.sql").write_text(SQL_MACROS)
    for name, source in TEMPLATES.items():
        path = tmp_path / "sql" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(sys.modules, "lsp_macros", raising=False)
    return tmp_path


@pytest.fixture
def assistant(project: Path) -> _Assistant:
    return _Assistant(load_project(project))


@pytest.fixture
def app(tmp_path: Path) -> Path:
    for name, source in APP.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


def test_a_template_found_in_the_code_is_checked(app: Path) -> None:
    helper = _Assistant(load_project(app))
    path = app / "shop" / "sql" / "users.sql"
    assert helper.diagnose(path, path.read_text()) == []
    [found] = helper.diagnose(path, "SELECT q.sided('up')")
    assert found.message == "q.sided: argument 1 must be 'left' or 'right', got 'up'"
    assert helper.definition("WHERE q.owned(:t)", 8) == Target(
        app / "shop" / "macros.py", 7, 4
    )


def test_a_good_template_has_no_problems(assistant: _Assistant, project: Path) -> None:
    path = project / "sql" / "good.sql"
    assert assistant.diagnose(path, path.read_text()) == []


def test_an_unknown_macro_is_marked_where_the_call_is(
    assistant: _Assistant, project: Path
) -> None:
    source = "SELECT 1\nWHERE tpl.nope(:x) AND TRUE"
    [found] = assistant.diagnose(project / "sql" / "new.sql", source)
    assert source[found.start : found.end] == "tpl.nope(:x)"
    assert found.message.startswith("unknown macro tpl.nope; available: ")


def test_an_argument_is_marked_where_it_is(
    assistant: _Assistant, project: Path
) -> None:
    source = "WHERE tpl.mine( teams )"
    [found] = assistant.diagnose(project / "sql" / "new.sql", source)
    assert found == Diagnostic(
        16, 21, "tpl.mine: argument 1 must be a `:parameter`, got 'teams'"
    )


def test_what_is_never_closed_is_marked_where_it_opens(
    assistant: _Assistant, project: Path
) -> None:
    source = "SELECT 1,\n  'open"
    [found] = assistant.diagnose(project / "sql" / "new.sql", source)
    assert (found.start, found.message) == (12, "a quoted string is never closed")


def test_a_problem_in_an_included_template_is_marked_on_the_include(
    assistant: _Assistant, project: Path
) -> None:
    path = project / "sql" / "outer.sql"
    source = path.read_text()
    [found] = assistant.diagnose(path, source)
    assert source[found.start : found.end] == "FROM tpl.include('inner.sql') AS i"
    assert "(included from outer.sql:2)" in found.message


def test_the_server_reads_the_sql_files_under_the_paths(
    assistant: _Assistant, project: Path
) -> None:
    assert assistant.applies_to(project / "sql" / "good.sql")
    assert assistant.applies_to(project / "sql" / "open.sql")
    assert not assistant.applies_to(project / "sql" / "notes.txt")
    assert not assistant.applies_to(project / "elsewhere.sql")


def test_macros_complete_after_the_namespace(assistant: _Assistant) -> None:
    source = "WHERE tpl.i"
    labels = [one.label for one in assistant.complete(source, len(source))]
    assert labels == [
        "if_set",
        "icontains",
        "icollate",
        "identifier",
        "in_list",
        "include",
    ]


def test_a_macro_completes_as_a_call_with_placeholders(assistant: _Assistant) -> None:
    source = "WHERE tpl.if_"
    assert assistant.complete(source, len(source)) == [
        Completion(
            "if_set",
            "macro",
            "tpl.if_set(:value, expr[, otherwise])",
            assistant.project.templates.macros["if_set"].doc,
            "if_set(:${1:value}, ${2:expr})",
        )
    ]


def test_an_include_completes_the_macro_templates(assistant: _Assistant) -> None:
    source = "FROM tpl.include('in"
    assert assistant.complete(source, len(source)) == [
        Completion("inner.sql", "template")
    ]


def test_a_parameter_completes_from_the_file(assistant: _Assistant) -> None:
    source = "WHERE a = :alpha AND b IN :beta AND c = :"
    labels = [one.label for one in assistant.complete(source, len(source))]
    assert labels == ["alpha", "beta"]
    assert assistant.complete("SELECT x::", 10) == []


def test_hover_shows_how_a_macro_is_called(assistant: _Assistant) -> None:
    source = "WHERE tpl.mine(:teams)"
    assert assistant.hover(source, 12) == (
        "```sql\ntpl.mine(:teams)\n```\n\nRows of any of the teams."
    )
    assert assistant.hover(source, 2) is None


def test_definition_goes_to_the_macro_and_to_the_included_file(
    assistant: _Assistant, project: Path
) -> None:
    assert assistant.definition("WHERE tpl.mine(:t)", 11) == Target(
        project / "lsp_macros.py", 5, 4
    )
    source = "FROM tpl.include('inner.sql') AS i"
    assert assistant.definition(source, 20) == Target(project / "sql" / "inner.sql", 0)


@pytest.mark.parametrize(
    ("source", "offset", "position"),
    [
        ("ab\ncd", 4, (1, 1)),
        ("имя\nx", 2, (0, 2)),
        ("😀x\ny", 1, (0, 2)),
        ("😀x\ny", 2, (0, 3)),
    ],
)
def test_positions_count_utf16_as_the_protocol_does(
    source: str, offset: int, position: tuple[int, int]
) -> None:
    assert position_of(source, offset) == position
    assert offset_of(source, *position) == offset


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"  # pygls runs on asyncio


@pytest.mark.anyio
async def test_the_server_answers_an_editor(project: Path) -> None:
    from lsprotocol import types
    from pygls.lsp.client import LanguageClient

    client = LanguageClient("test", "1")
    published: asyncio.Future[types.PublishDiagnosticsParams] = (
        asyncio.get_running_loop().create_future()
    )

    logged: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    @client.feature(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
    def diagnostics(params: types.PublishDiagnosticsParams) -> None:
        if not published.done():
            published.set_result(params)

    @client.feature(types.WINDOW_LOG_MESSAGE)
    def log(params: types.LogMessageParams) -> None:
        if not logged.done():
            logged.set_result(params.message)

    await client.start_io(sys.executable, "-m", "sqlakit_lsp", cwd=str(project))
    await client.initialize_async(
        types.InitializeParams(
            capabilities=types.ClientCapabilities(), root_uri=project.as_uri()
        )
    )
    client.initialized(types.InitializedParams())
    assert (await asyncio.wait_for(logged, 10)).splitlines() == [
        "templates: sql (db.py:12)",
        "namespace: tpl (the default)",
        "macros: 1 in Python, 1 file of SQL macros",
        "dialect: not in the code, so `sqlakit export` takes --dialect",
    ]
    uri = (project / "sql" / "new.sql").as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            types.TextDocumentItem(uri, "sql", 1, "SELECT 1\nWHERE tpl.nope(:x)")
        )
    )
    found = await asyncio.wait_for(published, 10)
    [diagnostic] = found.diagnostics
    assert (diagnostic.range.start.line, diagnostic.range.start.character) == (1, 6)
    assert diagnostic.message.startswith("unknown macro tpl.nope")

    completion = await client.text_document_completion_async(
        types.CompletionParams(types.TextDocumentIdentifier(uri), types.Position(1, 10))
    )
    assert isinstance(completion, types.CompletionList)
    assert "mine" in [item.label for item in completion.items]

    text = "SELECT 1 WHERE tpl.mine(:t)"
    mine = (project / "sql" / "mine.sql").as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(types.TextDocumentItem(mine, "sql", 1, text))
    )
    at = types.Position(0, text.index("mine") + 1)
    here = types.TextDocumentIdentifier(mine)
    places = [
        await client.text_document_definition_async(types.DefinitionParams(here, at)),
        await client.text_document_implementation_async(
            types.ImplementationParams(here, at)
        ),
        await client.text_document_declaration_async(types.DeclarationParams(here, at)),
    ]
    assert [
        (place.uri, place.range.start.line, place.range.start.character)
        for place in places
        if isinstance(place, types.Location)
    ] == [((project / "lsp_macros.py").resolve().as_uri(), 5, 4)] * 3

    referenced = await client.text_document_references_async(
        types.ReferenceParams(
            context=types.ReferenceContext(include_declaration=True),
            text_document=here,
            position=at,
        )
    )
    assert [
        (place.uri, place.range.start.line, place.range.start.character)
        for place in referenced or []
    ] == [
        ((project / "lsp_macros.py").resolve().as_uri(), 5, 4),
        ((project / "sql" / "good.sql").resolve().as_uri(), 1, 10),
    ]

    outer = (project / "sql" / "outer.sql").as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            types.TextDocumentItem(
                outer, "sql", 1, (project / "sql" / "outer.sql").read_text()
            )
        )
    )
    links = await client.text_document_document_link_async(
        types.DocumentLinkParams(types.TextDocumentIdentifier(outer))
    )
    assert [link.target for link in links or []] == [
        (project / "sql" / "inner.sql").resolve().as_uri()
    ]

    await client.shutdown_async(None)
    client.exit(None)
    await client.stop()


@pytest.mark.anyio
async def test_a_definition_is_a_link_for_an_editor_that_takes_one(
    project: Path,
) -> None:
    from lsprotocol import types
    from pygls.lsp.client import LanguageClient

    client = LanguageClient("test", "1")
    await client.start_io(sys.executable, "-m", "sqlakit_lsp", cwd=str(project))
    await client.initialize_async(
        types.InitializeParams(
            capabilities=types.ClientCapabilities(
                text_document=types.TextDocumentClientCapabilities(
                    definition=types.DefinitionClientCapabilities(link_support=True)
                )
            ),
            root_uri=project.as_uri(),
        )
    )
    client.initialized(types.InitializedParams())
    text = "SELECT 1 WHERE tpl.mine(:t)"
    uri = (project / "sql" / "mine.sql").as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(types.TextDocumentItem(uri, "sql", 1, text))
    )

    found = await client.text_document_definition_async(
        types.DefinitionParams(
            types.TextDocumentIdentifier(uri), types.Position(0, text.index("mine"))
        )
    )

    assert isinstance(found, list)
    [link] = found
    assert isinstance(link, types.LocationLink)
    origin = link.origin_selection_range
    assert origin is not None
    assert (origin.start.character, origin.end.character) == (15, 23)
    assert text[15:23] == "tpl.mine"

    await client.shutdown_async(None)
    client.exit(None)
    await client.stop()


def test_sql_macros_complete_and_hover_like_the_others(assistant: _Assistant) -> None:
    source = "WHERE tpl.for_"
    assert assistant.complete(source, len(source)) == [
        Completion(
            "for_team",
            "macro",
            "tpl.for_team(t)",
            "Rows of the team the call asks for.",
            "for_team(${1:t})",
        )
    ]
    assert assistant.hover("WHERE tpl.for_team(u)", 12) == (
        "On postgresql:\n\n```sql\n(u.team = :team)\n```\n\n"
        "```sql\ntpl.for_team(t)\n```\n\nRows of the team the call asks for."
    )


def test_an_sql_macro_is_defined_at_its_name(
    assistant: _Assistant, project: Path
) -> None:
    assert assistant.definition("WHERE tpl.visible(u)", 11) == Target(
        project / "_macros.sql", 3, 38
    )


def test_a_file_of_sql_macros_is_checked_as_it_stands(
    assistant: _Assistant, project: Path
) -> None:
    path = project / "_macros.sql"
    assert assistant.applies_to(path)
    assert assistant.diagnose(path, SQL_MACROS) == []
    broken = SQL_MACROS.replace("t.public", "tpl.nope(t)")
    [found] = assistant.diagnose(path, broken)
    assert broken[found.start : found.end] == (
        "SELECT tpl.for_team(t) OR tpl.nope(t) AS visible FROM t;"
    )
    assert found.message.startswith("Unknown macro tpl.nope in _macros.sql:4")


def test_a_template_calling_an_sql_macro_wrongly_is_marked(
    assistant: _Assistant, project: Path
) -> None:
    source = "SELECT * FROM users AS u WHERE tpl.visible(u, 1)"
    [found] = assistant.diagnose(project / "sql" / "new.sql", source)
    assert source[found.start : found.end] == "tpl.visible(u, 1)"
    assert found.message == "tpl.visible: takes 1 arguments, got 2"


def test_the_code_is_checked_for_templates_that_are_not_there(
    assistant: _Assistant,
) -> None:
    [found] = assistant.python_diagnose(CODE)
    assert CODE[found.start : found.end] == "missing.sql"
    assert found.message == "No SQL template named `missing.sql` in sql."


def test_a_template_name_in_the_code_goes_to_its_file(
    assistant: _Assistant, project: Path
) -> None:
    offset = CODE.index("good.sql") + 3
    assert assistant.python_definition(CODE, offset) == Target(
        project / "sql" / "good.sql", 0
    )
    assert assistant.python_definition(CODE, CODE.index("print")) is None


def test_a_template_name_completes_in_the_code(assistant: _Assistant) -> None:
    source = 'rows = db.sql("go'
    assert assistant.python_complete(source, len(source)) == [
        Completion("good.sql", "template")
    ]
    assert assistant.python_complete("print('go", 9) == []


def test_template_names_are_links(assistant: _Assistant, project: Path) -> None:
    code = project / "code.py"
    assert [
        (CODE[start:end], target.name)
        for start, end, target in assistant.links(code, CODE)
    ] == [("good.sql", "good.sql"), ("inner.sql", "inner.sql")]
    outer = project / "sql" / "outer.sql"
    source = outer.read_text()
    [(start, end, target)] = assistant.links(outer, source)
    assert (source[start:end], target) == (
        "inner.sql",
        project / "sql" / "inner.sql",
    )


def test_the_server_reads_the_python_of_the_project(
    assistant: _Assistant, project: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    assert assistant.reads_python(project / "code.py")
    assert not assistant.reads_python(project / "tests" / "test_code.py")
    assert not assistant.reads_python(tmp_path_factory.mktemp("elsewhere") / "x.py")
    assert not assistant.reads_python(project / "sql" / "good.sql")


def test_a_file_of_sql_macros_passes_arguments_where_parameters_go(
    assistant: _Assistant, project: Path
) -> None:
    source = (
        "SELECT tpl.if_set(negate, col NOT IN (vals), col IN (vals)) AS picked\n"
        "FROM col, vals, negate;\n"
    )
    assert assistant.diagnose(project / "_macros.sql", source) == []


def test_a_macro_over_several_lines_is_defined_at_its_alias(tmp_path: Path) -> None:
    path = tmp_path / "_macros.sql"
    path.write_text(
        "-- Several lines.\nSELECT tpl.if_set(\n    x, y\n) AS long_one\nFROM x, y;\n"
    )
    [macro] = sql_macros(path)
    assert _source_of(macro) == Target(path, 3, 5)


def test_a_column_goes_to_the_editor_in_utf16(tmp_path: Path) -> None:
    path = tmp_path / "_macros.sql"
    path.write_text("SELECT 'имя😀' = x AS named FROM x;\n")
    [macro] = sql_macros(path)
    target = _source_of(macro)
    assert target == Target(path, 0, 21)
    assert _utf16_column(target) == 22


HANDLERS = """from db import db

rows = db.sql("inner.sql").all()
"""


def places(project: Path, found: list[Reference]) -> list[tuple[str, str]]:
    """Each place as its file, and the text it names there."""
    return [
        (
            place.path.relative_to(project).as_posix(),
            place.path.read_text()[place.start : place.end],
        )
        for place in found
    ]


def test_a_macro_is_referenced_by_its_calls(
    assistant: _Assistant, project: Path
) -> None:
    good = project / "sql" / "good.sql"
    source = good.read_text()

    found = assistant.references(good, source, source.index("mine") + 1)

    assert places(project, found) == [("sql/good.sql", "mine")]


def test_a_macro_is_referenced_from_where_it_is_defined(
    assistant: _Assistant, project: Path
) -> None:
    python = project / "lsp_macros.py"
    macros = project / "_macros.sql"

    from_def = assistant.references(
        python, python.read_text(), python.read_text().index("def mine") + 5
    )
    from_alias = assistant.references(
        macros, macros.read_text(), macros.read_text().index("AS for_team") + 4
    )

    assert places(project, from_def) == [("sql/good.sql", "mine")]
    assert places(project, from_alias) == [("_macros.sql", "for_team")]


def test_a_template_is_referenced_by_what_reads_it(
    assistant: _Assistant, project: Path
) -> None:
    (project / "handlers.py").write_text(HANDLERS)
    inner = project / "sql" / "inner.sql"
    outer = project / "sql" / "outer.sql"
    expected = [("sql/outer.sql", "inner.sql"), ("handlers.py", "inner.sql")]

    anywhere = assistant.references(inner, inner.read_text(), 3)
    at_include = assistant.references(
        outer, outer.read_text(), outer.read_text().index("inner.sql")
    )
    at_call = assistant.references(
        project / "handlers.py", HANDLERS, HANDLERS.index("inner.sql") + 2
    )

    assert places(project, anywhere) == expected
    assert places(project, at_include) == expected
    assert places(project, at_call) == expected


def test_references_read_what_the_editor_holds(
    assistant: _Assistant, project: Path
) -> None:
    good = project / "sql" / "good.sql"
    text = "SELECT tpl.mine(:a), tpl.mine(:b)"
    first = text.index("mine")
    second = text.index("mine", first + 1)

    found = assistant.references(good, text, first, {good.resolve(): text})

    assert [(place.start, place.end) for place in found] == [
        (first, first + 4),
        (second, second + 4),
    ]


FILE_MACRO = '''from typing import Any

from sqlakit.sql import Param, Sql, sql_macro


@sql_macro("tenant.sql")
def of_team(row: Sql, team: Param) -> dict[str, Any]:
    """Rows of the team."""
    return {"team_id": team.value}
'''


def test_the_sql_of_a_file_macro_finds_its_calls(project: Path) -> None:
    (project / "tenant_macros.py").write_text(FILE_MACRO)
    (project / "tenant.sql").write_text(
        "SELECT row.team_id = :team_id AS of_team FROM row;\n"
    )
    (project / "sql" / "team.sql").write_text(
        "SELECT 1 FROM t WHERE tpl.of_team(t, :team)"
    )
    assistant = _Assistant(load_project(project))
    tenant = project / "tenant.sql"
    source = tenant.read_text()

    found = assistant.references(tenant, source, source.index("of_team") + 2)

    assert assistant.applies_to(tenant)
    assert assistant.diagnose(tenant, source) == []
    assert places(project, found) == [("sql/team.sql", "of_team")]


def test_references_read_a_file_again_only_when_it_changes(
    assistant: _Assistant, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlakit_lsp._server as server

    read: list[Path] = []
    original = server._read
    monkeypatch.setattr(
        server, "_read", lambda path: read.append(path) or original(path)
    )
    good = project / "sql" / "good.sql"
    source = good.read_text()
    at = source.index("mine")

    assistant.references(good, source, at)
    first = len(read)
    assistant.references(good, source, at)
    again = len(read) - first
    good.write_text(source + "\n  AND tpl.mine(:more)")
    os.utime(good, ns=(good.stat().st_atime_ns, good.stat().st_mtime_ns + 1_000_000))
    changed = assistant.references(good, good.read_text(), at)

    assert (first > 0, again) == (True, 0)
    assert places(project, changed) == [
        ("sql/good.sql", "mine"),
        ("sql/good.sql", "mine"),
    ]


def test_a_reloaded_project_keeps_the_files_it_read(
    assistant: _Assistant, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlakit_lsp._server as server

    scanned = list(assistant.scan_all())
    read: list[Path] = []
    original = server._read
    monkeypatch.setattr(
        server, "_read", lambda path: read.append(path) or original(path)
    )
    reloaded = _Assistant(load_project(project))
    reloaded.keep(assistant)
    good = project / "sql" / "good.sql"
    source = good.read_text()

    found = reloaded.references(good, source, source.index("mine"))

    assert good.resolve() in scanned
    assert read == []
    assert places(project, found) == [("sql/good.sql", "mine")]


def test_a_reloaded_project_keeps_its_files_until_one_is_made(
    assistant: _Assistant, project: Path
) -> None:
    list(assistant.scan_all())
    reloaded = _Assistant(load_project(project))
    reloaded.keep(assistant)
    made = project / "sql" / "made.sql"
    made.write_text("SELECT tpl.mine(:x)")
    kept = reloaded.names()

    listed_before = reloaded.lists(made)
    reloaded.forget_files()

    assert "made.sql" not in kept
    assert assistant.lists(project / "sql" / "good.sql")
    assert not listed_before
    assert reloaded.lists(made)
    assert "made.sql" in reloaded.names()


def test_a_template_is_named_however_the_call_is_spelled(
    assistant: _Assistant, project: Path
) -> None:
    code = 'rows = db . sql (\n    "inner.sql"\n).all()\nq = Q.from_sql("outer.sql")\n'
    handlers = project / "handlers.py"

    found = assistant.links(handlers, code)

    assert sorted(code[start:end] for start, end, _ in found) == [
        "inner.sql",
        "outer.sql",
    ]
    assert assistant.links(handlers, 'db.execute("inner.sql")') == []


def test_a_definition_starts_from_the_name_under_the_cursor(
    assistant: _Assistant,
) -> None:
    sql = "SELECT *\nFROM tpl.include('inner.sql') AS i\nWHERE tpl.identifier(:c, id)"
    code = 'rows = db.sql("inner.sql").all()'

    def spanned(source: str, offset: int, *, python: bool = False) -> str | None:
        span = assistant.origin(source, offset, python=python)
        return None if span is None else source[span[0] : span[1]]

    assert spanned(sql, sql.index("inner")) == "inner.sql"
    assert spanned(sql, sql.index("identifier") + 3) == "tpl.identifier"
    assert spanned(sql, sql.index("id)")) is None
    assert spanned(code, code.index("inner") + 1, python=True) == "inner.sql"


def test_a_macro_defined_under_the_cursor_is_its_own_definition(
    assistant: _Assistant, project: Path
) -> None:
    macros = project / "_macros.sql"
    sql = macros.read_text()
    python = project / "lsp_macros.py"
    code = python.read_text()
    at_alias = sql.index("AS for_team") + 5
    at_def = code.index("def mine") + 5

    alias = assistant.definition(sql, at_alias, macros)
    function = assistant.python_definition(code, at_def, python)

    assert alias == Target(macros, 1, sql.splitlines()[1].index("for_team"))
    assert function == Target(python, 5, 4)
    assert assistant.origin(sql, at_alias, path=macros) == (
        sql.index("for_team", at_alias - 5),
        sql.index("for_team", at_alias - 5) + len("for_team"),
    )
    assert assistant.definition(sql, sql.index("t.team"), macros) is None


def test_a_call_under_tpl_is_marked_when_the_namespace_is_another(
    project: Path,
) -> None:
    (project / "db.py").write_text(
        DB.replace('"_macros.sql"]', '"_macros.sql"], namespace="t"')
    )
    assistant = _Assistant(load_project(project))
    source = "SELECT 1 WHERE tpl.if_set(:a, TRUE)"

    [found] = assistant.diagnose(project / "sql" / "new.sql", source)

    assert source[found.start : found.end] == "tpl.if_set"
    assert found.message == (
        "`tpl.if_set` is not a macro call: the namespace is `t`, so write `t.if_set`"
    )


def test_a_file_macro_goes_between_its_function_and_its_sql(project: Path) -> None:
    (project / "tenant_macros.py").write_text(FILE_MACRO)
    (project / "tenant.sql").write_text(
        "-- Rows of the team.\nSELECT row.team_id = :team_id AS of_team FROM row;\n"
    )
    assistant = _Assistant(load_project(project))
    python, sql = project / "tenant_macros.py", project / "tenant.sql"
    code, text = python.read_text(), sql.read_text()

    from_sql = assistant.implementation(sql, text, text.index("of_team") + 1)
    from_python = assistant.implementation(python, code, code.index("def of_team") + 5)

    assert from_sql == Target(python, 6, 4)
    assert assistant.definition(text, text.index("of_team") + 1, sql) == from_sql
    assert from_python == Target(sql, 1, text.splitlines()[1].index("of_team"))
    assert assistant.implementation(sql, text, text.index("row.")) is None


def test_a_value_the_template_does_not_read_is_marked(assistant: _Assistant) -> None:
    code = 'db.sql("good.sql", teams=[1], teem=1)\n'
    passes_through = (
        'db.sql("good.sql", **values)\ndb.sql("good.sql", context, teem=1)\n'
    )

    [found] = assistant.python_diagnose(code)

    assert code[found.start : found.end] == "teem"
    assert found.message == (
        "`teem` is not a parameter of `good.sql`, which reads `q`, `teams`."
    )
    assert assistant.python_diagnose(passes_through) == []


def test_the_parameters_of_a_template_complete_in_its_call(
    assistant: _Assistant,
) -> None:
    code = 'rows = db.sql(\n    "good.sql", teams=[1], '

    offered = assistant.python_complete(code, len(code))
    typed = assistant.python_complete(code + "q", len(code) + 1)

    assert [(one.label, one.snippet) for one in offered] == [("q", "q=")]
    assert [one.label for one in typed] == ["q"]


def test_a_macro_s_arguments_show_while_they_are_written(
    assistant: _Assistant,
) -> None:
    written = "SELECT *\nWHERE tpl.if_set(:q, tpl.icontains(name, :q), 'a, b'"

    inner = assistant.signature(written, written.index("name") + 2)
    outer = assistant.signature(written, len(written))

    assert inner is not None
    assert (inner.label, inner.active) == ("tpl.icontains(column, text, collation)", 0)
    assert outer is not None
    assert (outer.label, outer.arguments, outer.active) == (
        "tpl.if_set(:value, expr, otherwise)",
        (":value", "expr", "otherwise"),
        2,
    )
    assert assistant.signature("SELECT count(", 13) is None


def test_hover_shows_the_sql_a_call_writes(assistant: _Assistant) -> None:
    source = "SELECT * FROM users WHERE tpl.if_set(:q, name = :q)"

    shown = assistant.hover(source, source.index("if_set"))

    assert shown == (
        "`:q` given:\n\n```sql\nname = :q\n```\n\n"
        "`:q` not given:\n\n```sql\nTRUE\n```\n\n"
        "```sql\ntpl.if_set(:value, expr[, otherwise])\n```\n\n"
        + assistant.project.templates.macros["if_set"].doc
    )


def renamed(project: Path, found: object) -> object:
    """Return a rename as its places, each a file and its text, and its move."""
    if isinstance(found, str):
        return found
    assert isinstance(found, tuple)
    spans, moved = found
    return (
        sorted(places(project, spans)),
        None
        if moved is None
        else tuple(path.relative_to(project).as_posix() for path in moved),
    )


def test_a_macro_is_renamed_where_it_is_defined_and_called(
    assistant: _Assistant, project: Path
) -> None:
    good = project / "sql" / "good.sql"
    source = good.read_text()
    macros = project / "_macros.sql"
    sql = macros.read_text()

    python_macro = assistant.rename(good, source, source.index("mine"), "ours")
    sql_macro = assistant.rename(macros, sql, sql.index("AS for_team") + 4, "by_team")

    assert renamed(project, python_macro) == (
        [("lsp_macros.py", "mine"), ("sql/good.sql", "mine")],
        None,
    )
    assert renamed(project, sql_macro) == (
        [("_macros.sql", "for_team"), ("_macros.sql", "for_team")],
        None,
    )


@pytest.mark.parametrize(
    ("new", "problem"),
    [
        ("if_set", "`if_set` is a macro already."),
        (
            "two words",
            "A macro's name is a word of letters, digits and `_`: `two words` is not.",
        ),
    ],
)
def test_a_rename_that_cannot_be_done_says_why(
    assistant: _Assistant, project: Path, new: str, problem: str
) -> None:
    good = project / "sql" / "good.sql"
    source = good.read_text()

    assert assistant.rename(good, source, source.index("mine"), new) == problem
    assert assistant.renamable(good, source, source.index("if_set")) == (
        "`if_set` is built in, and keeps its name."
    )


def test_a_template_is_renamed_with_everything_that_reads_it(
    assistant: _Assistant, project: Path
) -> None:
    (project / "handlers.py").write_text(HANDLERS)
    outer = project / "sql" / "outer.sql"
    source = outer.read_text()
    at = source.index("inner.sql")

    found = assistant.rename(outer, source, at, "reads/inner.sql")

    assert assistant.renamable(outer, source, at) == (at, at + len("inner.sql"))
    assert renamed(project, found) == (
        [("handlers.py", "inner.sql"), ("sql/outer.sql", "inner.sql")],
        ("sql/inner.sql", "sql/reads/inner.sql"),
    )
    assert (
        assistant.rename(outer, source, at, "good.sql")
        == "`good.sql` is a template already."
    )


@pytest.mark.anyio
async def test_a_rename_reaches_the_editor_as_one_edit(project: Path) -> None:
    from lsprotocol import types
    from pygls.exceptions import JsonRpcException
    from pygls.lsp.client import LanguageClient

    client = LanguageClient("test", "1")
    await client.start_io(sys.executable, "-m", "sqlakit_lsp", cwd=str(project))
    await client.initialize_async(
        types.InitializeParams(
            capabilities=types.ClientCapabilities(), root_uri=project.as_uri()
        )
    )
    client.initialized(types.InitializedParams())
    outer = project / "sql" / "outer.sql"
    text = outer.read_text()
    here = types.TextDocumentIdentifier(outer.as_uri())
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            types.TextDocumentItem(outer.as_uri(), "sql", 1, text)
        )
    )
    at = types.Position(*position_of(text, text.index("inner.sql")))

    edit = await client.text_document_rename_async(
        types.RenameParams(here, at, "reads/inner.sql")
    )
    with pytest.raises(JsonRpcException, match="Rename a macro, or the name"):
        await client.text_document_prepare_rename_async(
            types.PrepareRenameParams(here, types.Position(1, 5))
        )

    assert edit is not None
    changes = edit.document_changes or []
    [moved] = [one for one in changes if isinstance(one, types.RenameFile)]
    assert (moved.old_uri, moved.new_uri) == (
        (project / "sql" / "inner.sql").resolve().as_uri(),
        (project / "sql" / "reads" / "inner.sql").resolve().as_uri(),
    )
    await client.shutdown_async(None)
    client.exit(None)
    await client.stop()


def test_a_built_in_macro_is_referenced_from_its_def(
    assistant: _Assistant, project: Path
) -> None:
    import sqlakit._sql

    library = Path(sqlakit._sql.__file__)
    source = library.read_text()
    at = source.index("def if_set(") + len("def ")

    found = assistant.references(library, source, at)

    assert assistant.defines_macros(library)
    assert not assistant.defines_macros(project / "sql" / "good.sql")
    assert places(project, found) == [("sql/good.sql", "if_set")]
    assert assistant.python_definition(source, at, library) == Target(
        library, source.count("\n", 0, at), len("def ")
    )


def test_a_text_is_read_once_whatever_asks_for_it(
    assistant: _Assistant, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loads: list[str] = []
    project_type = type(assistant.project)
    load = project_type.load

    def counted(self: object, name: str, source: str) -> object:
        loads.append(source)
        return load(self, name, source)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(project_type, "load", counted)
    good = project / "sql" / "good.sql"
    source = good.read_text()
    broken = "SELECT tpl.nope(1)"

    assistant.diagnose(good, source)
    assistant.hover(source, source.index("if_set"), good)
    assistant.diagnose(good, source)
    assistant.diagnose(good, broken)
    assistant.diagnose(good, broken)

    assert loads.count(source) == 1
    assert loads.count(broken) == 1


def test_a_template_is_read_again_when_what_it_includes_changes(
    assistant: _Assistant, project: Path
) -> None:
    outer = project / "sql" / "outer.sql"
    inner = project / "sql" / "inner.sql"
    inner.write_text("SELECT 1 AS id")
    source = outer.read_text()
    first = assistant.compiled("outer.sql", source)

    inner.write_text("SELECT 2 AS id")
    os.utime(inner, (inner.stat().st_atime, inner.stat().st_mtime + 1))

    assert assistant.compiled("outer.sql", source) is not first
    assert assistant.compiled("outer.sql", source) is assistant.compiled(
        "outer.sql", source
    )


def test_a_whole_template_renders_with_every_part_and_its_placeholders(
    assistant: _Assistant, project: Path
) -> None:
    (project / "sql" / "rows.sql").write_text(
        "SELECT * FROM tpl.values(:rows) AS v WHERE tpl.if_set(:q, name = :q)\n"
        "  AND tpl.mine(:teams)"
    )
    rows = project / "sql" / "rows.sql"

    rendered = assistant.rendered(rows, rows.read_text())

    assert rendered == (
        "-- rows.sql on postgresql\n"
        "SELECT * FROM (VALUES (:rows__1)) AS v WHERE name = :q\n"
        "  AND tpl.mine(:teams)\n"
    )


@pytest.mark.anyio
async def test_the_rendered_template_opens_in_the_editor(project: Path) -> None:
    from lsprotocol import types
    from pygls.lsp.client import LanguageClient

    client = LanguageClient("test", "1")
    shown: list[str] = []

    @client.feature(types.WINDOW_SHOW_DOCUMENT)
    def show(params: types.ShowDocumentParams) -> types.ShowDocumentResult:
        shown.append(params.uri)
        return types.ShowDocumentResult(success=True)

    await client.start_io(sys.executable, "-m", "sqlakit_lsp", cwd=str(project))
    await client.initialize_async(
        types.InitializeParams(
            capabilities=types.ClientCapabilities(
                window=types.WindowClientCapabilities(
                    show_document=types.ShowDocumentClientCapabilities(support=True)
                )
            ),
            root_uri=project.as_uri(),
        )
    )
    client.initialized(types.InitializedParams())
    good = project / "sql" / "good.sql"
    uri = good.as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            types.TextDocumentItem(uri, "sql", 1, good.read_text())
        )
    )

    actions = await client.text_document_code_action_async(
        types.CodeActionParams(
            types.TextDocumentIdentifier(uri),
            types.Range(types.Position(0, 0), types.Position(0, 0)),
            types.CodeActionContext(diagnostics=[]),
        )
    )
    assert [action.title for action in actions or []] == ["Show rendered SQL"]
    await client.workspace_execute_command_async(
        types.ExecuteCommandParams(RENDER, [uri])
    )

    [opened] = shown
    written = Path(unquote(urlparse(opened).path))
    assert written.name == "good.sql"
    text = await asyncio.to_thread(written.read_text)
    assert text.startswith("-- good.sql on postgresql\n")
    await client.shutdown_async(None)
    client.exit(None)
    await client.stop()


@pytest.mark.anyio
async def test_the_rendered_template_is_an_edit_for_an_editor_that_opens_none(
    project: Path,
) -> None:
    from lsprotocol import types
    from pygls.lsp.client import LanguageClient

    client = LanguageClient("test", "1")
    await client.start_io(sys.executable, "-m", "sqlakit_lsp", cwd=str(project))
    started = await client.initialize_async(
        types.InitializeParams(
            capabilities=types.ClientCapabilities(
                text_document=types.TextDocumentClientCapabilities(
                    code_action=types.CodeActionClientCapabilities(
                        resolve_support=types.ClientCodeActionResolveOptions(
                            properties=["edit"]
                        )
                    )
                )
            ),
            root_uri=project.as_uri(),
        )
    )
    client.initialized(types.InitializedParams())
    offered = started.capabilities.code_action_provider
    assert isinstance(offered, types.CodeActionOptions)
    assert offered.resolve_provider
    good = project / "sql" / "good.sql"
    uri = good.as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            types.TextDocumentItem(uri, "sql", 1, good.read_text())
        )
    )
    everything = types.Range(types.Position(0, 0), types.Position(0, 0))

    actions = await client.text_document_code_action_async(
        types.CodeActionParams(
            types.TextDocumentIdentifier(uri),
            everything,
            types.CodeActionContext(diagnostics=[]),
        )
    )
    chosen = types.CodeAction(title="Show rendered SQL", data={"uri": uri})
    resolved = await client.code_action_resolve_async(chosen)

    assert [action.title for action in actions or []] == ["Show rendered SQL"]
    assert resolved.edit is not None
    made, filled = resolved.edit.document_changes or []
    assert isinstance(made, types.CreateFile)
    assert isinstance(filled, types.TextDocumentEdit)
    assert made.uri == filled.text_document.uri
    assert made.uri.endswith("/good.sql")
    [text] = filled.edits
    assert isinstance(text, types.TextEdit)
    assert text.new_text.startswith("-- good.sql on postgresql\n")
    await client.shutdown_async(None)
    client.exit(None)
    await client.stop()


def fixed(source: str, found: list[Fix]) -> list[tuple[str, str]]:
    """Return each fix as its title, and the text with it written in."""
    return [
        (fix.title, source[: fix.start] + fix.text + source[fix.end :]) for fix in found
    ]


def test_a_problem_offers_the_names_it_could_have_meant(
    assistant: _Assistant, project: Path
) -> None:
    template = project / "sql" / "new.sql"
    macro = "SELECT 1 WHERE tpl.if_sett(:a, TRUE)"
    code = 'db.sql("goood.sql", teams=[1], team=1)\n'
    [unknown] = assistant.diagnose(template, macro)
    [missing] = assistant.python_diagnose(code)

    def offered(source: str, problem: Diagnostic) -> list[tuple[str, str]]:
        return fixed(
            source,
            assistant.fixes(source, problem.start, problem.end, problem.message),
        )

    assert offered(macro, unknown)[0] == (
        "Write `tpl.if_set`",
        "SELECT 1 WHERE tpl.if_set(:a, TRUE)",
    )
    assert offered(code, missing) == [
        ("Write `good.sql`", 'db.sql("good.sql", teams=[1], team=1)\n')
    ]


def test_a_value_the_template_does_not_read_offers_one_it_does(
    assistant: _Assistant,
) -> None:
    code = 'db.sql("good.sql", teams=[1], qq=1)\n'
    [extra] = assistant.python_diagnose(code)

    found = assistant.fixes(code, extra.start, extra.end, extra.message)

    assert fixed(code, found) == [("Write `q`", 'db.sql("good.sql", teams=[1], q=1)\n')]


def test_a_call_under_another_namespace_is_fixed_to_this_one(project: Path) -> None:
    (project / "db.py").write_text(
        DB.replace('"_macros.sql"]', '"_macros.sql"], namespace="t"')
    )
    assistant = _Assistant(load_project(project))
    source = "SELECT 1 WHERE tpl.if_set(:a, TRUE)"
    [found] = assistant.diagnose(project / "sql" / "new.sql", source)

    fix = assistant.fixes(source, found.start, found.end, found.message)

    assert fixed(source, fix) == [
        ("Write `t.if_set`", "SELECT 1 WHERE t.if_set(:a, TRUE)")
    ]


def test_a_file_outlines_what_it_holds(assistant: _Assistant, project: Path) -> None:
    macros = project / "_macros.sql"
    good = project / "sql" / "good.sql"
    outer = project / "sql" / "outer.sql"

    def outline(path: Path) -> list[tuple[str, str, str]]:
        source = path.read_text()
        return [
            (one.name, one.kind, source[one.start : one.end])
            for one in assistant.symbols(path, source)
        ]

    assert outline(macros) == [
        ("for_team", "macro", "for_team"),
        ("visible", "macro", "visible"),
    ]
    assert outline(good) == [
        ("tpl.mine", "macro", "tpl.mine"),
        ("tpl.if_set", "macro", "tpl.if_set"),
        (":teams", "parameter", ":teams"),
        (":q", "parameter", ":q"),
    ]
    assert outline(outer) == [("inner.sql", "template", "inner.sql")]


def test_the_project_is_searched_for_macros_and_templates(
    assistant: _Assistant,
) -> None:
    found = [(name, kind) for name, kind, _, _ in assistant.workspace_symbols("in")]

    assert ("tpl.mine", "macro") in found
    assert ("tpl.in_list", "macro") in found
    assert ("inner.sql", "template") in found
    assert all("in" in name.lower() for name, _ in found)


def test_a_parameter_hovers_with_the_calls_that_pass_it(
    assistant: _Assistant, project: Path
) -> None:
    (project / "handlers.py").write_text(
        "from db import db\n\n"
        'one = db.sql("good.sql", teams=[1], q="a").all()\n'
        'two = db.sql("good.sql", teams=[2]).all()\n'
        'three = db.sql("good.sql", **values).all()\n'
    )
    assistant.forget_files()
    good = project / "sql" / "good.sql"
    source = good.read_text()

    shown = assistant.parameter_hover(good, source, source.index(":q") + 1)

    assert shown == (
        "`:q` of `good.sql`\n\n"
        "Passed by 2:\n\n- `handlers.py:3`\n- `handlers.py:5`\n\n"
        "Not passed by 1:\n\n- `handlers.py:4`"
    )
    assert assistant.parameter_hover(good, source, source.index("SELECT")) is None


@pytest.mark.anyio
async def test_a_quick_fix_comes_with_the_problem_it_fixes(project: Path) -> None:
    from lsprotocol import types
    from pygls.lsp.client import LanguageClient

    client = LanguageClient("test", "1")
    published: asyncio.Future[types.PublishDiagnosticsParams] = (
        asyncio.get_running_loop().create_future()
    )

    @client.feature(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
    def diagnostics(params: types.PublishDiagnosticsParams) -> None:
        if not published.done():
            published.set_result(params)

    await client.start_io(sys.executable, "-m", "sqlakit_lsp", cwd=str(project))
    await client.initialize_async(
        types.InitializeParams(
            capabilities=types.ClientCapabilities(), root_uri=project.as_uri()
        )
    )
    client.initialized(types.InitializedParams())
    uri = (project / "sql" / "typo.sql").as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            types.TextDocumentItem(
                uri, "sql", 1, "SELECT 1 WHERE tpl.if_sett(:a, TRUE)"
            )
        )
    )
    [problem] = (await asyncio.wait_for(published, 10)).diagnostics

    actions = await client.text_document_code_action_async(
        types.CodeActionParams(
            types.TextDocumentIdentifier(uri),
            problem.range,
            types.CodeActionContext(diagnostics=[problem]),
        )
    )

    assert [action.title for action in actions or []] == [
        "Write `tpl.if_set`",
        "Show rendered SQL",
    ]
    await client.shutdown_async(None)
    client.exit(None)
    await client.stop()


def test_a_parameter_no_call_passes_is_marked(
    assistant: _Assistant, project: Path
) -> None:
    (project / "handlers.py").write_text(
        "from db import db\n\n"
        'one = db.sql("outer.sql", teams=[1]).all()\n'
        'two = db.sql("inner.sql", x=1).all()\n'
    )
    (project / "sql" / "inner.sql").write_text("SELECT id FROM t WHERE id = :x")
    assistant.forget_files()
    inner = project / "sql" / "inner.sql"
    typo = "SELECT id FROM t WHERE team IN (:teem) AND id = :x LIMIT :limit"

    found = assistant.diagnose(inner, typo)

    assert [
        (typo[one.start : one.end], one.severity, one.message) for one in found
    ] == [
        (":limit", "hint", "No call in the project's Python passes `:limit`."),
        (
            ":teem",
            "warning",
            "No call passes `:teem`, and the calls pass `teams`, `x`.",
        ),
    ]
    [warning] = [one for one in found if one.severity == "warning"]
    assert fixed(
        typo, assistant.fixes(typo, warning.start, warning.end, warning.message)
    ) == [
        ("Write `:teams`", typo.replace(":teem", ":teams")),
    ]


def test_nothing_is_marked_when_a_call_passes_values_it_does_not_name(
    assistant: _Assistant, project: Path
) -> None:
    (project / "handlers.py").write_text(
        'from db import db\n\nrows = db.sql("good.sql", **values).all()\n'
    )
    assistant.forget_files()
    good = project / "sql" / "good.sql"

    assert assistant.diagnose(good, good.read_text() + " AND :other") == []


def test_a_context_written_out_names_its_values_as_keywords_do(
    assistant: _Assistant, project: Path
) -> None:
    code = (
        'db.sql("good.sql", {"teams": [1], "qq": 1})\n'
        'db.sql("good.sql", context={"q": 1})\n'
        'db.sql("good.sql", context)\n'
    )
    good = project / "sql" / "good.sql"
    (project / "handlers.py").write_text(code.replace("context)\n", "{})\n"))
    assistant.forget_files()

    found = assistant.python_diagnose(code)

    assert [(code[one.start : one.end], one.message) for one in found] == [
        ("qq", "`qq` is not a parameter of `good.sql`, which reads `q`, `teams`.")
    ]
    assert assistant.passed("good.sql") == {"teams", "qq", "q"}
    assert assistant.diagnose(good, good.read_text()) == []
