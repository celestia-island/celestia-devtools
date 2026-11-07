"""Documentation tooling: Markdown formatter/linter and the protocol-doc vendor.

  protocol-bundle — vendor the five org legal docs into a webui .generated area.

  markdown   — formatting engine (headings, fences, tables, lists, blockquotes)
  linter/    — language-aware checks, split by concern:
      fence     programming-language inference for code blocks
      i18n      cross-language duplicate-paragraph detection
      tabs      tab-character warnings
      external  markdownlint-cli2 bridge
"""
