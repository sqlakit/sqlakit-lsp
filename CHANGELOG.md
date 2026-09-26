# Changelog

## 0.1.0

The first release: a language server for SQLAKit templates, run as
`sqlakit-lsp` over stdio. It reads the project the way `sqlakit check` does,
and gives an editor the problems in a template as you type, completion, hover
with the SQL a call writes, go to definition, references and rename, the
macros' calls and the parameters coloured, quick fixes, the outline and a
search of the project, and **Show rendered SQL**. It needs `sqlakit` 0.21.
