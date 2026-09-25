# sqlakit-lsp

The language server for [SQLAKit](https://sqlakit.readthedocs.io/en/stable/)
templates: the problems `sqlakit check` finds, as you type, completion after
`tpl.`, hover and go to definition.

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

The [documentation](https://sqlakit.readthedocs.io/en/stable/sql/#editor-support)
covers what it reads and how to set it up.

## Development

```console
$ uv sync
$ uv run poe test
$ uv run poe lint
```

Until the next `sqlakit` release, `sqlakit` is read from `../sqlakit`.
