"""Language-aware lint checks for Markdown documentation.

Modules:
  fence     — programming-language inference for code fences
  i18n      — cross-language duplicate-paragraph (translation) detection
  tabs      — tab-character warnings
  external  — markdownlint-cli2 bridge
"""

from celestia_devtools.doc.linter.external import maybe_run_markdownlint
from celestia_devtools.doc.linter.fence import infer_fence_language
from celestia_devtools.doc.linter.i18n import check_duplicate_paragraphs
from celestia_devtools.doc.linter.tabs import check_tabs

__all__ = [
    "infer_fence_language",
    "check_tabs",
    "check_duplicate_paragraphs",
    "maybe_run_markdownlint",
]
