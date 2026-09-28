"""Small MySQL-aware SQL text helpers: tokenizing, statement splitting,
comment stripping, fingerprints and identifier extraction.

Not a parser. It knows enough about quotes and comments to split
statements safely and to find names, which is all this tool needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CODE, STRING, IDENT, COMMENT, SEMI = "code", "string", "ident", "comment", "semi"


def tokenize(sql: str) -> list[tuple[str, str]]:
    """Split SQL into (kind, text) chunks. Concatenating all texts gives back `sql`."""
    out: list[tuple[str, str]] = []
    n = len(sql)
    i = start = 0

    def flush(end: int) -> None:
        if end > start:
            out.append((CODE, sql[start:end]))

    while i < n:
        c = sql[i]
        if c in "'\"`":
            flush(i)
            j = i + 1
            while j < n:
                d = sql[j]
                if d == "\\" and c != "`":
                    j += 2
                    continue
                if d == c:
                    if j + 1 < n and sql[j + 1] == c:  # doubled quote = escaped quote
                        j += 2
                        continue
                    break
                j += 1
            j = min(j + 1, n)
            out.append((IDENT if c == "`" else STRING, sql[i:j]))
            i = start = j
            continue
        if c == "#" or (
            c == "-"
            and sql.startswith("--", i)
            and (i + 2 == n or sql[i + 2] in " \t\r\n")
        ):
            flush(i)
            j = sql.find("\n", i)
            j = n if j == -1 else j  # the newline itself stays code
            out.append((COMMENT, sql[i:j]))
            i = start = j
            continue
        if c == "/" and sql.startswith("/*", i):
            flush(i)
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append((COMMENT, sql[i:j]))
            i = start = j
            continue
        if c == ";":
            flush(i)
            out.append((SEMI, ";"))
            i = start = i + 1
            continue
        i += 1
    flush(n)
    return out


@dataclass
class Statement:
    raw: str  # original text including comments, without the trailing ';'
    code: str  # comments removed, stripped


def _code_of(tokens: list[tuple[str, str]]) -> str:
    return "".join(" " if k == COMMENT else t for k, t in tokens).strip()


def split_statements(sql: str) -> list[Statement]:
    stmts: list[Statement] = []
    cur: list[tuple[str, str]] = []

    def emit() -> None:
        code = _code_of(cur)
        if code:
            stmts.append(Statement("".join(t for _, t in cur), code))

    for kind, text in tokenize(sql):
        if kind == SEMI:
            emit()
            cur = []
        else:
            cur.append((kind, text))
    emit()
    return stmts


def strip_comments(sql: str) -> str:
    return _code_of(tokenize(sql))


def split_leading_comments(sql: str) -> tuple[str, str]:
    """Return (leading comments/whitespace, rest)."""
    pos = 0
    for kind, text in tokenize(sql):
        if kind == COMMENT or (kind == CODE and not text.strip()):
            pos += len(text)
            continue
        if kind == CODE:
            pos += len(text) - len(text.lstrip())
        break
    return sql[:pos], sql[pos:]


_PUNCT_WS = re.compile(r"\s*([(),.=;])\s*")


def fingerprint(sql: str) -> str:
    """Formatting- and comment-insensitive form of a statement, for change detection."""
    parts = []
    for kind, text in tokenize(sql):
        if kind == COMMENT:
            parts.append(" ")
        elif kind == CODE:
            parts.append(text)
        elif kind == SEMI:
            parts.append(";")
        else:
            parts.append(f"\0{text}\0")  # protect literals from whitespace folding
    joined = "".join(parts)
    chunks = joined.split("\0")
    for i in range(0, len(chunks), 2):  # even chunks are code
        c = re.sub(r"\s+", " ", chunks[i])
        chunks[i] = _PUNCT_WS.sub(r"\1", c)
    return "".join(chunks).strip().rstrip(";").strip()


_WORD = re.compile(r"(?<![\w$@])[A-Za-z_$][\w$]*")


def unquote_ident(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] == "`":
        return name[1:-1].replace("``", "`")
    return name


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def identifiers(sql: str) -> set[str]:
    """All identifier-like words (lowercased) outside comments and string literals."""
    found: set[str] = set()
    for kind, text in tokenize(sql):
        if kind == IDENT:
            found.add(unquote_ident(text).lower())
        elif kind == CODE:
            found.update(w.lower() for w in _WORD.findall(text))
    return found


# ---- names in DDL -----------------------------------------------------------

_Q = r"(?:`(?:[^`]|``)+`|[\w$]+)"
QNAME = rf"{_Q}(?:\s*\.\s*{_Q})?"
_Q_RE = re.compile(_Q)


def last_name_part(qname: str) -> str:
    return unquote_ident(_Q_RE.findall(qname)[-1])


_DDL = [
    (
        "altered",
        re.compile(
            rf"^ALTER\s+(?:ONLINE\s+|IGNORE\s+)*TABLE\s+({QNAME})", re.IGNORECASE
        ),
    ),
    (
        "created",
        re.compile(
            rf"^CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({QNAME})",
            re.IGNORECASE,
        ),
    ),
    (
        "dropped",
        re.compile(
            rf"^DROP\s+(?:TEMPORARY\s+)?TABLES?\s+(?:IF\s+EXISTS\s+)?({QNAME}(?:\s*,\s*{QNAME})*)",
            re.IGNORECASE,
        ),
    ),
]
_RENAME = re.compile(r"^RENAME\s+TABLES?\s+(.+)$", re.IGNORECASE | re.DOTALL)
_RENAME_PAIR = re.compile(rf"({QNAME})\s+TO\s+({QNAME})", re.IGNORECASE)
_QNAME_RE = re.compile(QNAME)


def tables_touched(stmt_code: str) -> list[tuple[str, str]]:
    """(action, table_name) pairs for table-level DDL in one statement."""
    for action, rx in _DDL:
        m = rx.match(stmt_code)
        if m:
            return [(action, last_name_part(q)) for q in _QNAME_RE.findall(m.group(1))]
    m = _RENAME.match(stmt_code)
    if m:
        out = []
        for old, new in _RENAME_PAIR.findall(m.group(1)):
            out += [("renamed", last_name_part(old)), ("renamed", last_name_part(new))]
        return out
    return []


CREATE_VIEW_RE = re.compile(
    r"^CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:ALGORITHM\s*=\s*\w+\s+)?(?:DEFINER\s*=\s*\S+\s+)?(?:SQL\s+SECURITY\s+\w+\s+)?"
    rf"VIEW\s+({QNAME})",
    re.IGNORECASE,
)
DROP_VIEW_RE = re.compile(
    rf"^DROP\s+VIEW\s+(?:IF\s+EXISTS\s+)?({QNAME}(?:\s*,\s*{QNAME})*)", re.IGNORECASE
)
_CREATE_PREFIX = re.compile(r"^CREATE\s+(?:OR\s+REPLACE\s+)?", re.IGNORECASE)


def as_create_or_replace(stmt_text: str) -> str:
    return _CREATE_PREFIX.sub("CREATE OR REPLACE ", stmt_text, count=1)


def dropped_views(stmt_code: str) -> list[str] | None:
    m = DROP_VIEW_RE.match(stmt_code)
    if not m:
        return None
    return [last_name_part(q) for q in _QNAME_RE.findall(m.group(1))]
