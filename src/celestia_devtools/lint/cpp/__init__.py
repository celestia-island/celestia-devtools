"""C++ support layer for the cpp-style layout checker.

The package is split so each piece can be tested on its own:

- tokenizer -- the source scanner (comments, strings, raw strings, character
  literals, numbers, preprocessor directives, exact token positions);
- structure -- lightweight structure on top of the token stream (delimiter
  matching, brace/paren depth, blank lines, macro names defined by #define);
- rules -- the rule catalogue (id, source clause, severity, judgment,
  exceptions) shared by list-rules and the configuration validator;
- config -- fail-closed cpp-style.toml / [tool.cpp_style] loading;
- checks -- one function per rule, all read-only.

The CLI entry point is celestia_devtools.lint.cpp_style; nothing here writes to
disk.
"""
