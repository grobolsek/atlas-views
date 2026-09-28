"""View files, Atlas migration files, marker blocks and the view state they encode."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from .sqltext import (
    CREATE_VIEW_RE,
    as_create_or_replace,
    dropped_views,
    fingerprint,
    identifiers,
    last_name_part,
    quote_ident,
    split_leading_comments,
    split_statements,
    tables_touched,
)

BEGIN = "-- atlas-views:begin"
END = "-- atlas-views:end"
BLOCK_RE = re.compile(r"^-- atlas-views:begin\b.*?^-- atlas-views:end[^\n]*(?:\n|\Z)", re.MULTILINE | re.DOTALL)
DELIMITER_RE = re.compile(r"^--\s*atlas:delimiter\b", re.MULTILINE)
_BODY_START = re.compile(r"^(?:SELECT|WITH|\()", re.IGNORECASE)


class ProjectError(Exception):
    pass


@dataclass
class ViewDef:
    name: str  # display name, unquoted
    sql: str  # full CREATE OR REPLACE ... statement without trailing ';'
    source: str  # view file path, or migration file name

    @property
    def key(self) -> str:
        return self.name.lower()

    @cached_property
    def fingerprint(self) -> str:
        return fingerprint(self.sql)

    @cached_property
    def idents(self) -> set[str]:
        return identifiers(self.sql) - {self.key}


# ---- views folder -------------------------------------------------------------


def parse_view_file(path: Path) -> ViewDef:
    text = path.read_text(encoding="utf-8")
    stmts = split_statements(text)
    if not stmts:
        raise ProjectError(f"{path}: file is empty")
    if len(stmts) > 1:
        raise ProjectError(f"{path}: expected exactly one statement, found {len(stmts)}")
    st = stmts[0]
    lead, rest = split_leading_comments(st.raw)
    rest = rest.rstrip()
    m = CREATE_VIEW_RE.match(st.code)
    if m:
        name = last_name_part(m.group(1))
        sql = lead + as_create_or_replace(rest)
    elif _BODY_START.match(st.code):
        name = path.stem
        sql = f"{lead}CREATE OR REPLACE VIEW {quote_ident(name)} AS\n{rest}"
    else:
        raise ProjectError(f"{path}: expected `CREATE VIEW name AS ...` or a bare SELECT/WITH query")
    return ViewDef(name=name, sql=sql.strip(), source=str(path))


def load_views(views_dir: Path) -> dict[str, ViewDef]:
    if not views_dir.is_dir():
        raise ProjectError(f"views folder not found: {views_dir}")
    views: dict[str, ViewDef] = {}
    for path in sorted(views_dir.rglob("*.sql")):
        v = parse_view_file(path)
        if v.key in views:
            raise ProjectError(f"view `{v.name}` is defined twice: {views[v.key].source} and {path}")
        views[v.key] = v
    return views


# ---- migrations ---------------------------------------------------------------


@dataclass
class Op:
    action: str  # "create" | "drop"
    key: str
    view: ViewDef | None  # for create
    name: str


@dataclass
class MigrationFile:
    path: Path
    text: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def version(self) -> str:
        return self.path.stem.split("_", 1)[0]

    @cached_property
    def _blocks(self) -> list[re.Match]:
        return list(BLOCK_RE.finditer(self.text))

    @property
    def has_block(self) -> bool:
        if len(self._blocks) > 1:
            raise ProjectError(f"{self.name}: more than one atlas-views block")
        if not self._blocks and BEGIN in self.text:
            raise ProjectError(f"{self.name}: `{BEGIN}` without a matching `{END}`")
        return bool(self._blocks)

    @property
    def block_text(self) -> str:
        return self._blocks[0].group(0) if self.has_block else ""

    @property
    def text_without_block(self) -> str:
        return BLOCK_RE.sub("", self.text) if self.has_block else self.text

    @property
    def uses_custom_delimiter(self) -> bool:
        return bool(DELIMITER_RE.search(self.text_without_block))

    @cached_property
    def ops(self) -> list[Op]:
        ops: list[Op] = []
        for st in split_statements(self.block_text):
            m = CREATE_VIEW_RE.match(st.code)
            if m:
                name = last_name_part(m.group(1))
                v = ViewDef(name=name, sql=st.code, source=self.name)
                ops.append(Op("create", v.key, v, name))
                continue
            names = dropped_views(st.code)
            if names is not None:
                ops += [Op("drop", n.lower(), None, n) for n in names]
                continue
            raise ProjectError(f"{self.name}: unexpected statement in atlas-views block: {st.code[:60]}...")
        return ops

    def table_changes(self) -> list[tuple[str, str]]:
        """(action, table) for table DDL outside the atlas-views block."""
        out = []
        for st in split_statements(self.text_without_block):
            out += tables_touched(st.code)
        return out


def load_migrations(migrations_dir: Path) -> list[MigrationFile]:
    if not migrations_dir.is_dir():
        raise ProjectError(f"migrations folder not found: {migrations_dir}")
    return [MigrationFile(p, p.read_text(encoding="utf-8")) for p in sorted(migrations_dir.glob("*.sql"))]


def files_upto(files: list[MigrationFile], version: str | None) -> list[MigrationFile]:
    """Files up to and including `version` (None = no migrations applied)."""
    if version is None:
        return []
    for i, f in enumerate(files):
        if f.version == version:
            return files[: i + 1]
    raise ProjectError(f"version {version} not found in migrations folder")


# ---- state ----------------------------------------------------------------------


@dataclass
class ViewState:
    views: dict[str, ViewDef] = field(default_factory=dict)  # ordered by last definition
    history: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # key -> [(file, action)]


def compute_state(files: list[MigrationFile]) -> ViewState:
    state = ViewState()
    for f in files:
        for op in f.ops:
            hist = state.history.setdefault(op.key, [])
            if op.action == "create":
                hist.append((f.name, "create" if op.key not in state.views else "replace"))
                state.views.pop(op.key, None)
                state.views[op.key] = op.view
            else:
                hist.append((f.name, "drop"))
                state.views.pop(op.key, None)
    return state


def managed_views(files: list[MigrationFile]) -> set[str]:
    """Every view key that any atlas-views block ever created or dropped."""
    return {op.key for f in files for op in f.ops}


def topo_order(views: dict[str, ViewDef], subset: list[str] | None = None) -> tuple[list[str], bool]:
    """Order `subset` (default: all) so views come after the views they reference.
    Returns (order, had_cycle).
    """
    keys = list(views) if subset is None else list(subset)
    inset = set(keys)
    deps = {k: views[k].idents & inset for k in keys}
    done: list[str] = []
    placed: set[str] = set()
    remaining = keys[:]
    while remaining:
        for k in remaining:
            if deps[k] <= placed:
                done.append(k)
                placed.add(k)
                remaining.remove(k)
                break
        else:
            return done + remaining, True
    return done, False
