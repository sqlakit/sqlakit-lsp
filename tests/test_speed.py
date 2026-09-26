"""What the server costs per request, on a project of 600 templates.

An editor asks on every keystroke and every move of the cursor, so each request
the editor sends that often has a budget. A change that goes over it fails here,
not in someone's editor.
"""

import itertools
import statistics
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlakit._project import load_project

from sqlakit_lsp._server import _Assistant, position_of

BUDGET = 0.015
"""Seconds a request may take, as the median of several: generous for a slower
machine in CI, and still well under what an editor makes a person wait for."""

TEMPLATES = 600
PYTHON_FILES = 80
MACROS = 40
INSTALLED = 3000
"""Python files in the virtual environment, which the server never reads."""


@pytest.fixture(scope="module")
def large(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write a project with a template directory, macros and code that reads them."""
    root = tmp_path_factory.mktemp("large")
    (root / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (root / "app" / "sql").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    macros = ["from sqlakit.sql import Param, Sql, sql_macro\n"]
    macros.extend(
        f"\n\n@sql_macro\ndef m{index}(x: Param, *cols: Sql) -> str:\n"
        f'    """Macro {index}."""\n    return "TRUE"\n'
        for index in range(MACROS)
    )
    (root / "app" / "macros.py").write_text("".join(macros))
    (root / "app" / "db.py").write_text(
        "from pathlib import Path\n\nfrom sqlakit import Database\n"
        "from sqlakit.sql import Templates\n\n"
        'db = Database("postgresql://x/y", templates=Templates('
        'Path(__file__).parent / "sql", macros=["app.macros"], engine="tpl"))\n'
    )
    (root / "app" / "sql" / "_macros.sql").write_text(
        "\n".join(
            f"-- Macro s{index}.\nSELECT t.a = {index} AS s{index} FROM t;\n"
            for index in range(20)
        )
    )
    for number in range(TEMPLATES):
        folder, index = divmod(number, 30)
        (root / "app" / "sql" / f"d{folder}").mkdir(exist_ok=True)
        include = (
            f"\nJOIN tpl.include('d{folder + 1}/q{index}.sql') AS inc ON TRUE"
            if index % 5 == 0 and folder < TEMPLATES // 30 - 1
            else ""
        )
        (root / "app" / "sql" / f"d{folder}" / f"q{index}.sql").write_text(
            f"SELECT a, b, c\nFROM t{number} AS t{include}\n"
            f"WHERE tpl.if_set(:a, t.a = :a)\n  AND tpl.m{number % MACROS}(:x, t.a, t.b)\n"
            f"  AND tpl.s{number % 20}(t)\nORDER BY tpl.order_by(:sort, a, b, c)\nLIMIT :limit\n"
        )
    installed = root / ".venv" / "lib" / "site-packages" / "package"
    installed.mkdir(parents=True)
    for index in range(INSTALLED):
        (installed / f"module_{index}.py").write_text('db.sql("d0/q0.sql")\n')
    for file in range(PYTHON_FILES):
        calls = [
            f'def f{index}():\n    return db.sql("d{n // 30}/q{n % 30}.sql", a=1).all()\n\n'
            for index, n in (
                (index, (file * 15 + index) % TEMPLATES) for index in range(15)
            )
        ]
        (root / "app" / f"handlers_{file}.py").write_text(
            "from app.db import db\n\n" + "".join(calls)
        )
    return root


@pytest.fixture(scope="module")
def assistant(large: Path) -> _Assistant:
    helper = _Assistant(load_project(large))
    for _ in helper.scan_all():
        pass
    return helper


def _median(call: Callable[[], object], times: int = 15) -> float:
    call()
    spent = []
    for _ in range(times):
        start = time.perf_counter()
        call()
        spent.append(time.perf_counter() - start)
    return statistics.median(spent)


def test_the_requests_an_editor_sends_most_stay_within_budget(
    assistant: _Assistant, large: Path
) -> None:
    template = large / "app" / "sql" / "d0" / "q0.sql"
    source = template.read_text()
    python = large / "app" / "handlers_0.py"
    code = python.read_text()
    macro = source.index("m0(") + 1
    keys = itertools.count()
    macros = large / "app" / "sql" / "_macros.sql"
    broken = source.replace("tpl.m0(", "tpl.m00(")
    [unknown] = assistant.diagnose(template, broken)
    requests = {
        # A keystroke makes new text, which no cache holds.
        "diagnose": lambda: assistant.diagnose(template, f"{source}-- {next(keys)}"),
        "complete": lambda: assistant.complete(source + "\ntpl.", len(source) + 5),
        "hover": lambda: assistant.hover(source, macro, template),
        "signature": lambda: assistant.signature(source, source.index("t.a, t.b")),
        "definition": lambda: assistant.definition(source, macro, template),
        "links": lambda: assistant.links(template, source),
        "references": lambda: assistant.references(template, source, macro),
        "python_diagnose": lambda: assistant.python_diagnose(code),
        "python_complete": lambda: assistant.python_complete('db.sql("d1/', 11),
        "include_complete": lambda: assistant.complete(
            "FROM tpl.include('d1/", len("FROM tpl.include('d1/")
        ),
        "fixes": lambda: assistant.fixes(
            broken, unknown.start, unknown.end, unknown.message
        ),
        "outline": lambda: assistant.symbols(template, source),
        "outline_macros": lambda: assistant.symbols(macros, macros.read_text()),
        "search": lambda: assistant.workspace_symbols("m1"),
        "tokens": lambda: assistant.tokens(source),
        "parameter_hover": lambda: assistant.parameter_hover(
            template, source, source.index(":a")
        ),
    }

    spent = {name: _median(call) for name, call in requests.items()}

    assert {name: seconds for name, seconds in spent.items() if seconds > BUDGET} == {}


def test_a_file_made_costs_the_next_keystroke_little(
    assistant: _Assistant, large: Path
) -> None:
    template = large / "app" / "sql" / "d0" / "q0.sql"
    source = template.read_text()
    keys = itertools.count()

    def made() -> None:
        assistant.forget_files()
        assistant.diagnose(template, f"{source}-- {next(keys)}")

    # The list of files is read again, and not the virtual environment's.
    assert _median(made, times=5) < BUDGET * 3


def test_a_long_template_is_coloured_within_budget(assistant: _Assistant) -> None:
    line = "  AND tpl.if_set(:a, t.a = :a) AND tpl.m1(:x, t.b)\n"
    source = "SELECT *\nFROM t\nWHERE TRUE\n" + line * 3000

    def coloured() -> None:
        for start, end, _, _ in assistant.tokens(source):
            position_of(source, start)
            position_of(source, end)

    assert _median(coloured, times=5) < BUDGET * 3
