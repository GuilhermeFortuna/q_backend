"""Sphinx configuration for Q Research Library."""

from __future__ import annotations

import sys
from pathlib import Path

# Add q_backend/src to sys.path so autodoc can import q_backend.research
SRC_PATH = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC_PATH))

project = "Q Research"
copyright = "2026, Q Quantitative Platform"
author = "Q Team"
version = "0.1.0"
release = "0.1.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.intersphinx",
    "sphinx.ext.mathjax",
    "numpydoc",
    "myst_parser",
    "sphinx_copybutton",
    "sphinx_design",
]

# Numpydoc settings (matching pandas)
numpydoc_show_class_members = False
numpydoc_class_members_toctree = False

# Autosummary generation
autosummary_generate = True
autosummary_imported_members = False

# Autodoc settings
autodoc_typehints = "none"

# PyData Theme settings
html_theme = "pydata_sphinx_theme"
html_title = "Q Research"
templates_path = ["_templates"]
html_static_path = ["_static"]
html_css_files = ["research.css"]
html_show_sourcelink = False
html_last_updated_fmt = None
html_context = {"default_mode": "dark"}

html_theme_options = {
    "github_url": "https://github.com/GuilhermeFortuna/q_backend",
    "show_prev_next": True,
    "navbar_start": ["navbar-logo"],
    "navbar_center": ["navbar-nav"],
    "navbar_end": ["theme-switcher", "navbar-icon-links"],
    "footer_start": ["copyright"],
    "footer_end": ["sphinx-version", "theme-version"],
    "show_toc_level": 2,
    "secondary_sidebar_items": ["page-toc"],
    "navigation_with_keys": True,
}

# Intersphinx mapping to link pandas/numpy/python standard library
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pandas": ("https://pandas.pydata.org/pandas-docs/stable/", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
}

# MyST Markdown parser support
myst_enable_extensions = ["colon_fence", "deflist", "dollarmath"]

# Exclude patterns
exclude_patterns = ["_build", "README.md", "Thumbs.db", ".DS_Store"]

# Keep copied examples executable when the API reference uses Python prompts.
copybutton_prompt_text = r">>> |\.\.\. "
copybutton_prompt_is_regexp = True
