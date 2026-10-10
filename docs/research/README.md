# Research documentation

The public research library documentation uses Sphinx, the PyData theme,
NumPy-style docstrings, and autosummary, following the structure of pandas docs.

Run from `q_backend`:

```bash
make docs-check   # Build HTML; warnings fail the check
make docs-serve   # Build and preview at http://127.0.0.1:8088
make docs-clean   # Remove HTML, Sphinx cache, and generated API pages
```

`uv run --group docs` installs the locked documentation dependencies as needed.
The first build needs network access for Python, pandas, and NumPy intersphinx
inventories. The documentation build imports the research modules but does not
fetch market data or require running Q services.

- `getting_started/` teaches the first complete workflow.
- `user_guide/` explains behavior and practical use.
- `reference/` groups the public API. Its `api/` pages are generated and ignored;
  edit source docstrings or `_templates/autosummary/`, not generated pages.
- `_static/research.css` refines the reading layout and mirrors Q's carbon,
  brass, cream, and silver palette from `q_frontend/src/styles/globals.css`.
  Dark mode is the default; the theme switcher also offers a warm light mode.
  The palette is copied deliberately so the docs build stays independent of
  the frontend checkout.

When changing a class template, run `make docs-clean` before `make docs-check`
so existing autosummary pages are regenerated. Check examples against the public
API and keep service requirements, timezones, units, and fill timing explicit.
