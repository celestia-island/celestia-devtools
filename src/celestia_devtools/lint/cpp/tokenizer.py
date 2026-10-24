#!/usr/bin/env python3
"""C++ tokenizer used by the cpp-style layout checker.

*Why a tokenizer:* every rule in the C++ catalogue is stated in terms of "code
tokens on a line" -- an identifier directly followed by an opening parenthesis,
semicolons inside a for header, a block comment versus a license header. A regex
over the raw source cannot tell those apart from text inside a string, a raw
string, a comment or a preprocessor line, so the checker first splits the file
into tokens with exact source positions and only then applies rules.

The scanner handles the lexical traps a layout checker must survive:

- line comments, including a comment that is continued onto the next physical
  line with a trailing backslash;
- block comments (they do not nest, and may span lines);
- ordinary strings with escapes, adjacent string literals, and the u8/u/U/L
  prefixes;
- raw strings with a delimiter of up to 16 characters, including embedded
  quotes, parentheses and newlines;
- character literals, including escaped quotes and escaped backslashes;
- C++14 digit separators (for example 1'000 or 0xFF'FF): the apostrophe inside
  a number must not be taken for the start of a character literal;
- preprocessor directives (a # that is the first non-whitespace character of a
  physical line), with backslash-newline continuations, and #if / #ifdef /
  #else / #endif nesting;
- template angle brackets: here they are ordinary < and > operators, which is
  enough because no rule in the catalogue needs to distinguish a template
  argument list from a comparison, while brace/paren/bracket matching is not
  affected by them;
- UTF-8 (invalid bytes are replaced and reported), a leading BOM, CRLF line
  endings, tabs and form feeds.

Token contract:

- kind is one of IDENT, NUMBER, STRING, CHAR, OP, COMMENT, PREPROC, NEWLINE;
- line/col are 1-based; col counts characters, so a tab is one column;
  end_line/end_col are exclusive (the position just past the token);
- significant keeps every token except comments and preprocessor directives
  (newlines are kept); code additionally drops newlines and is the sequence the
  structural rules walk;
- a preprocessor directive's token text runs from # to the end of its logical
  line (continuation newlines included) and excludes the final newline.

The tokenizer never raises on malformed input: unterminated strings, comments,
raw strings and conditionals are emitted with an entry in Tokenized.warnings.
"""

from __future__ import annotations
from bisect import bisect_right
from dataclasses import dataclass
from typing import List, Optional, Tuple

IDENT = "IDENT"
NUMBER = "NUMBER"
STRING = "STRING"
CHAR = "CHAR"
OP = "OP"
COMMENT = "COMMENT"
PREPROC = "PREPROC"
NEWLINE = "NEWLINE"

#: Token kinds that are lexical trivia rather than code.
TRIVIA = frozenset({COMMENT, PREPROC})

_STRING_PREFIXES = frozenset({"u8", "u", "U", "L"})
_RAW_PREFIXES = frozenset({"R", "u8R", "uR", "UR", "LR"})

# Longest match first. Digraphs that could matter for delimiter matching are
# tokenized as ordinary operators, which is enough for the rules implemented
# here.
_OPERATORS_THREE = ("<<=", ">>=", "<=>", "->*", "...", "##")
_OPERATORS_TWO = ("::", "->", "++", "--", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", ".*")

_CONDITIONAL_STARTS = frozenset({"if", "ifdef", "ifndef"})

@dataclass(frozen = True)
class Token:
    """One lexical token; end_line/end_col are exclusive."""

    kind: str
    text: str
    line: int
    col: int
    end_line: int
    end_col: int

@dataclass(frozen = True)
class Directive:
    """A preprocessor directive occurrence with its conditional nesting depth.

    depth is the nesting depth that is entered (for #if / #ifdef / #ifndef), the
    depth currently open (for #else / #elif and all non-conditional directives),
    or the depth being closed (for #endif).
    """

    name: str
    line: int
    depth: int

@dataclass
class Tokenized:
    """Result of tokenizing one source text."""

    text: str
    tokens: List[Token]
    significant: List[Token]
    code: List[Token]
    directives: List[Directive]
    warnings: List[str]

def _normalize(text: str) -> str:
    """Strip a UTF-8 BOM and normalize CRLF / bare CR line endings to LF."""

    if text.startswith("\ufeff"):
        text = text[1:]

    return text.replace("\r\n", "\n").replace("\r", "\n")

def split_lines(text: str) -> List[str]:
    """Physical lines without terminators (the trailing newline adds no line)."""

    if not text:
        return []

    lines = text.split("\n")

    if lines and (lines[-1] == ""):
        lines.pop()

    return lines

def _is_ident_start(char: str) -> bool:
    return (char == "_") or char.isalpha()

def _is_ident_char(char: str) -> bool:
    return (char == "_") or char.isalnum()

class _Locator:
    """Map absolute string offsets to 1-based (line, col) positions."""

    __slots__ = ("starts",)

    def __init__(self, text: str) -> None:
        starts = [0]

        for offset, char in enumerate(text):
            if char == "\n":
                starts.append(offset + 1)

        self.starts = starts

    def line_col(self, offset: int) -> Tuple[int, int]:
        index = bisect_right(self.starts, offset) - 1

        if index < 0:
            index = 0

        return index + 1, offset - self.starts[index] + 1

class _Scanner:
    """One-pass scanner; all state lives on the instance."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.length = len(text)
        self.position = 0
        self.tokens: List[Token] = []
        self.directives: List[Directive] = []
        self.warnings: List[str] = []
        self.locator = _Locator(text)
        self.conditional_stack: List[str] = []

    # -- helpers ---------------------------------------------------------
    def _peek(self, offset: int) -> str:
        index = self.position + offset
        return self.text[index] if index < self.length else ""

    def _warn(self, offset: int, message: str) -> None:
        line, _ = self.locator.line_col(offset)
        self.warnings.append("line %d: %s" % (line, message))

    def _emit(self, kind: str, start: int, end: int) -> Token:
        line, col = self.locator.line_col(start)
        end_line, end_col = self.locator.line_col(end)
        token = Token(kind, self.text[start:end], line, col, end_line, end_col)
        self.tokens.append(token)
        return token

    def _at_line_start(self, offset: int) -> bool:
        line_start = self.text.rfind("\n", 0, offset) + 1
        return not self.text[line_start:offset].strip()

    def _consume_suffix(self, end: int) -> int:
        while (end < self.length) and _is_ident_char(self.text[end]):
            end += 1

        return end

    # -- literal scanners -------------------------------------------------
    def _line_comment(self) -> None:
        start = self.position
        cursor = start + 2

        while True:
            newline = self.text.find("\n", cursor)

            if newline < 0:
                end = self.length
                break

            if (newline > start) and (self.text[newline - 1] == "\\"):
                cursor = newline + 1
                continue

            end = newline
            break

        self._emit(COMMENT, start, end)
        self.position = end

    def _block_comment(self) -> None:
        start = self.position
        closing = self.text.find("*/", start + 2)

        if closing < 0:
            self._warn(start, "unterminated block comment")
            end = self.length
        else:
            end = closing + 2

        self._emit(COMMENT, start, end)
        self.position = end

    def _string_body(self, quote_index: int) -> int:
        """Scan an ordinary string starting at quote_index; return exclusive end."""
        index = quote_index + 1

        while index < self.length:
            char = self.text[index]

            if char == "\\":
                index += 2 if (index + 1) < self.length else 1
            elif char == '"':
                return self._consume_suffix(index + 1)
            elif char == "\n":
                self._warn(quote_index, "unterminated string literal")
                return index
            else:
                index += 1

        self._warn(quote_index, "unterminated string literal")
        return self.length

    def _char_body(self, quote_index: int) -> int:
        """Scan a character literal starting at quote_index; return exclusive end."""
        index = quote_index + 1

        while index < self.length:
            char = self.text[index]

            if (char == "\\") and ((index + 1) < self.length):
                index += 2
                continue

            if char == "'":
                return self._consume_suffix(index + 1)

            if char == "\n":
                self._warn(quote_index, "unterminated character literal")
                return index

            index += 1

        self._warn(quote_index, "unterminated character literal")
        return self.length

    def _raw_string(self, start: int, quote_index: int) -> Optional[int]:
        """Scan a raw string; return exclusive end, or None for a bad opener."""
        index = quote_index + 1
        delimiter_start = index

        while (index < self.length) and ((index - delimiter_start) <= 16):
            char = self.text[index]

            if char == "(":
                delimiter = self.text[delimiter_start:index]
                closer = ")" + delimiter + '"'
                closing = self.text.find(closer, index + 1)

                if closing < 0:
                    self._warn(start, "unterminated raw string literal")
                    return self.length

                return self._consume_suffix(closing + len(closer))

            if (char in " \t\v\f\n\\()") or not char.isprintable():
                return None

            index += 1

        return None

    def _number(self) -> None:
        start = self.position
        index = start

        if self.text[index] == ".":
            index += 1

        while index < self.length:
            char = self.text[index]

            if char == "'":
                following = self.text[index + 1] if (index + 1) < self.length else ""

                if following.isascii() and following.isalnum():
                    index += 2
                    continue

                break

            if char.isascii() and (char.isalnum() or (char in "_.")):
                index += 1
                continue

            if (char in "+-") and (index > start) and (self.text[index - 1] in "eEpP"):
                index += 1
                continue

            break

        self._emit(NUMBER, start, index)
        self.position = index

    def _identifier_or_literal(self) -> None:
        start = self.position
        end = start

        while (end < self.length) and _is_ident_char(self.text[end]):
            end += 1

        word = self.text[start:end]
        following = self.text[end] if end < self.length else ""

        if (following == '"') and (word in _RAW_PREFIXES):
            raw_end = self._raw_string(start, end)

            if raw_end is not None:
                self._emit(STRING, start, raw_end)
                self.position = raw_end
                return
        elif (following == '"') and (word in _STRING_PREFIXES):
            string_end = self._string_body(end)
            self._emit(STRING, start, string_end)
            self.position = string_end
            return
        elif (following == "'") and (word in _STRING_PREFIXES):
            char_end = self._char_body(end)
            self._emit(CHAR, start, char_end)
            self.position = char_end
            return

        self._emit(IDENT, start, end)
        self.position = end

    def _preproc(self) -> None:
        start = self.position
        cursor = start + 1

        while True:
            newline = self.text.find("\n", cursor)

            if newline < 0:
                end = self.length
                break

            if (newline > start) and (self.text[newline - 1] == "\\"):
                cursor = newline + 1
                continue

            end = newline
            break

        token = self._emit(PREPROC, start, end)
        self._record_directive(token)
        self.position = end

    def _record_directive(self, token: Token) -> None:
        body = token.text.lstrip()

        if body.startswith("#"):
            body = body[1:].lstrip()

        index = 0

        while (index < len(body)) and body[index].isascii() and body[index].isalpha():
            index += 1

        name = body[:index]

        if name in _CONDITIONAL_STARTS:
            self.conditional_stack.append(name)
            depth = len(self.conditional_stack)
        elif name == "endif":
            depth = len(self.conditional_stack)

            if self.conditional_stack:
                self.conditional_stack.pop()
            else:
                self.warnings.append("line %d: stray #endif without #if" % token.line)
        elif name in ("else", "elif"):
            depth = len(self.conditional_stack)

            if not self.conditional_stack:
                self.warnings.append("line %d: stray #%s without #if" % (token.line, name))
        else:
            depth = len(self.conditional_stack)

        self.directives.append(Directive(name, token.line, depth))

    def _operator(self) -> None:
        start = self.position

        for operator in _OPERATORS_THREE:
            if self.text.startswith(operator, start):
                self._emit(OP, start, start + 3)
                self.position = start + 3
                return

        for operator in _OPERATORS_TWO:
            if self.text.startswith(operator, start):
                self._emit(OP, start, start + 2)
                self.position = start + 2
                return

        self._emit(OP, start, start + 1)
        self.position = start + 1

    # -- main loop --------------------------------------------------------
    def run(self) -> Tokenized:
        while self.position < self.length:
            char = self.text[self.position]
            following = self._peek(1)

            if char == "\n":
                self._emit(NEWLINE, self.position, self.position + 1)
                self.position += 1
            elif char in " \t\v\f":
                self.position += 1
            elif (char == "/") and (following == "/"):
                self._line_comment()
            elif (char == "/") and (following == "*"):
                self._block_comment()
            elif char == '"':
                end = self._string_body(self.position)
                self._emit(STRING, self.position, end)
                self.position = end
            elif char == "'":
                end = self._char_body(self.position)
                self._emit(CHAR, self.position, end)
                self.position = end
            elif _is_ident_start(char):
                self._identifier_or_literal()
            elif (char.isascii() and char.isdigit()) or ((char == ".") and following.isascii() and following.isdigit()):
                self._number()
            elif (char == "#") and self._at_line_start(self.position):
                self._preproc()
            else:
                self._operator()

        if self.conditional_stack:
            line = self.locator.line_col(self.length)[0]
            self.warnings.append("line %d: unterminated preprocessor conditional (#if depth %d)" % (line, len(self.conditional_stack)))

        significant = [token for token in self.tokens if token.kind not in TRIVIA]
        code = [token for token in significant if token.kind != NEWLINE]
        return Tokenized(text = self.text, tokens = self.tokens, significant = significant, code = code, directives = self.directives, warnings = self.warnings)

def tokenize(text: str) -> Tokenized:
    """Tokenize text (BOM stripped, CRLF normalized)."""
    return _Scanner(_normalize(text)).run()

def tokenize_bytes(data: bytes) -> Tokenized:
    """Decode UTF-8 data (invalid bytes replaced with a warning) and tokenize it."""
    decode_warnings: List[str] = []

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", "replace")
        decode_warnings.append("invalid UTF-8 byte sequence(s) replaced with U+FFFD")

    tokenized = tokenize(text)

    if decode_warnings:
        tokenized.warnings = decode_warnings + tokenized.warnings

    return tokenized
