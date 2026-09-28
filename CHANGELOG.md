# Changelog

## Unreleased

- The server needs `sqlakit` 0.22.4 or newer: it renders a template with
  `sqlakit`'s own code, the code `sqlakit render` and `sqlakit check --lint`
  run, where it had a copy of its own.

## 0.3.2

- A macro call written on several lines, `tpl.order_by(` with a column on
  each line after it, renders in **Show rendered SQL** and in its hover. A
  call that could not be made was taken for SQL, as it comes back on one
  line.

## 0.3.1

- **Show rendered SQL** writes `tpl.order_by(:sort, ...)` as the sort it
  falls back to, `ORDER BY name ASC`, and not as the call. A parameter a
  built-in macro cannot take made up renders as not given.
- **Show rendered SQL** renders the macros of the project's Python too,
  `tpl.search(:q, u.name, u.email)` as the `LIKE`s it writes. The server
  reads those macros and does not run them, so it renders the template again
  with the project's `.venv/bin/python`, which imports them. Only this action
  runs the project's code, and only when the template calls such a macro.
  A macro a macro calls inside, in a file macro's SQL too, renders at any
  depth. Without a `.venv`, or when a module fails to import, the call stays
  a call and a comment on top says why.
- **Show rendered SQL with ?** writes a `?` for each parameter, and a comment
  on top lists them in order: `-- ? in order: :teams, :page_size`.

## 0.3.0

- Go to definition on a `:parameter` goes to the calls of Python that pass
  it, and selects `page_size` in `db.sql("users/search.sql",
  page_size=limit)`. A key of a context written out, `{"page_size": 20}`,
  counts the same. When no call names it, it goes to the code that may pass
  it: `values` in `**values`, `ctx` in `context=ctx`. A template another
  includes goes to the calls of that one.
- The hover of a `:parameter` shows what each call passes, `page_size=limit`
  or `**values`, on the line where it is written.

## 0.2.0

- The file `@sql_macro("tenant.sql")` names is a link to that SQL, next to
  the module, and go to definition on it opens the file.
- A keystroke after a file is made or removed costs a third of what it did:
  the templates are listed in one walk of the directories. 0.1.2 was meant to
  ship this and didn't.

## 0.1.2

- Go to definition on a built-in macro opens the `sqlakit` in the project's
  `.venv`, when it's the version the server runs. A server that `uvx` runs
  opened its own copy in uv's cache, where the project's settings don't apply.

## 0.1.1

- Runs with `sqlakit` 0.22 as well as 0.21.

## 0.1.0

The first release: a language server for SQLAKit templates, run as
`sqlakit-lsp` over stdio. It reads the project the way `sqlakit check` does,
and gives an editor the problems in a template as you type, completion, hover
with the SQL a call writes, go to definition, references and rename, the
macros' calls and the parameters coloured, quick fixes, the outline and a
search of the project, and **Show rendered SQL**. It needs `sqlakit` 0.21.
