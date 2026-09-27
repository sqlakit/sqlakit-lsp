# Changelog

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
