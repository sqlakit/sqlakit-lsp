# sqlakit-lsp

The language server for [SQLAKit](https://sqlakit.readthedocs.io/en/stable/)
templates. In a `.sql` template:

- the problems `sqlakit check` finds, as you type
- completion after `tpl.` and in `tpl.include('`, and a macro's arguments while
  you write them
- on hover, the SQL a call writes, above the macro's signature and docstring
- go to definition, implementation and references of a macro or a template
- rename of a macro or a template across the project
- the macros' calls and the parameters coloured
- a parameter no call of the project passes, marked, with a quick fix for a
  name close to one the calls pass
- **Show rendered SQL**, the whole template as the SQL it writes
- the outline of a template, and a search for a macro or a template

In Python, the template name in `db.sql("...")` completes and links to its
file, and a value the template does not read is marked.

```console
$ pip install sqlakit-lsp
```

Register `sqlakit-lsp` for `.sql` and `.py` files in your editor. It talks over
stdio. In Neovim 0.11:

```lua
vim.lsp.config("sqlakit", {
  cmd = { "sqlakit-lsp" },
  filetypes = { "sql", "python" },
  root_markers = { "pyproject.toml" },
})
vim.lsp.enable("sqlakit")
```

Zed colours the macros' calls and the parameters of a template from the
server once its settings say `"semantic_tokens": "combined"`.

The [documentation](https://sqlakit.readthedocs.io/en/stable/sql/#editor-support)
covers what it reads and how to set it up.

## Development

```console
$ uv sync
$ uv run poe test
$ uv run poe lint
```

Until the next `sqlakit` release, `sqlakit` is read from `../sqlakit`.
