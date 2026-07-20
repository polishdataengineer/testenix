"""Sphinx configuration for the Testenix documentation site."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

with (ROOT / "pyproject.toml").open("rb") as source:
    release = tomllib.load(source)["project"]["version"]

project = "Testenix"
author = "Dominik Franczyk and Testenix contributors"
copyright = "2026, Dominik Franczyk and Testenix contributors"
version = release

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

source_suffix = {".md": "markdown"}
master_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

myst_enable_extensions = [
    "attrs_inline",
    "colon_fence",
    "deflist",
    "fieldlist",
]
myst_heading_anchors = 3

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_typehints_format = "short"

html_theme = "furo"
html_title = f"Testenix {release}"
html_baseurl = "https://polishdataengineer.github.io/testenix/"
html_favicon = "_static/favicon.svg"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_js_files = ["copy-llm.js"]
html_extra_path = ["llms.txt", "llms-full.txt"]
html_copy_source = True
html_show_sourcelink = True

html_theme_options = {
    "source_repository": "https://github.com/polishdataengineer/testenix/",
    "source_branch": "main",
    "source_directory": "docs/",
    "light_css_variables": {
        "color-brand-primary": "#2563eb",
        "color-brand-content": "#1d4ed8",
        "color-api-name": "#7c3aed",
        "color-api-pre-name": "#475569",
    },
    "dark_css_variables": {
        "color-brand-primary": "#60a5fa",
        "color-brand-content": "#93c5fd",
        "color-api-name": "#c4b5fd",
        "color-api-pre-name": "#cbd5e1",
    },
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/polishdataengineer/testenix",
            "html": (
                '<svg stroke="currentColor" fill="currentColor" stroke-width="0" '
                'viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.64 0 8.13c0 '
                "3.59 2.29 6.64 5.47 7.71.4.08.55-.18.55-.39 0-.19-.01-.83-.01-1.5"
                "-2.01.44-2.53-.87-2.69-1.67-.09-.2-.48-.82-.82-.98-.28-.15-.68"
                "-.53-.01-.54.63-.01 1.08.59 1.23.84.72 1.23 1.87.88 2.33.67.07"
                "-.53.28-.88.51-1.08-1.6-.19-3.28-.81-3.28-3.58 0-.79.28-1.44.74"
                "-1.95-.07-.19-.32-.98.07-2.04 0 0 .6-.2 1.98.75A6.7 6.7 0 0 1 "
                "8 4.82c.68 0 1.36.09 2 .27 1.38-.95 1.98-.75 1.98-.75.39 1.06.14"
                " 1.85.07 2.04.46.51.74 1.16.74 1.95 0 2.78-1.69 3.39-3.29 "
                "3.58.29.25.54.73.54 1.48 0 1.08-.01 1.95-.01 2.22 0 .22.15.47.55"
                '.39A8.14 8.14 0 0 0 16 8.13C16 3.64 12.42 0 8 0Z"/></svg>'
            ),
            "class": "",
        }
    ],
}
