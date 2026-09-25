"""The server: what an editor is told about a project's templates."""

import asyncio
import sys
from pathlib import Path

import pytest
from sqlakit._project import load_project
from sqlakit._sql import sql_macros

from sqlakit_lsp._server import (
    Completion,
    Diagnostic,
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
    assert found.message == "q.sided: argument 1 is 'left' or 'right', got 'up'"
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
        16, 21, "tpl.mine: argument 1 must be a :parameter, got 'teams'"
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

    found = assistant.references(
        good, text, first, lambda path: text if path == good else path.read_text()
    )

    assert [(place.start, place.end) for place in found] == [
        (first, first + 4),
        (second, second + 4),
    ]
