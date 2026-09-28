from pathlib import Path

import pytest

from atlas_views.db import parse_url
from atlas_views.project import MigrationFile, ProjectError, compute_state, parse_view_file, topo_order
from atlas_views.sqltext import fingerprint, identifiers, split_statements, tables_touched
from atlas_views.sync import plan_sync


# ---- sqltext ------------------------------------------------------------------------

def test_split_respects_quotes_and_comments():
    sql = """
    -- a comment; with semicolon
    SELECT 'a;b', "c;d", `e;f` FROM t; /* x; y */
    # hash comment;
    SELECT 'it''s', 'x\\';y';
    """
    stmts = split_statements(sql)
    assert [s.code for s in stmts] == ["SELECT 'a;b', \"c;d\", `e;f` FROM t", "SELECT 'it''s', 'x\\';y'"]


def test_double_dash_without_space_is_not_a_comment():
    assert split_statements("SELECT 1--1;")[0].code == "SELECT 1--1"


def test_fingerprint_ignores_formatting_but_not_literals():
    a = "CREATE OR REPLACE VIEW v AS\n  SELECT a, b\n  FROM t -- note\n WHERE x = 'A  B'"
    b = "CREATE OR REPLACE VIEW v AS SELECT a,b FROM t WHERE x='A  B';"
    c = "CREATE OR REPLACE VIEW v AS SELECT a,b FROM t WHERE x='A B'"
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(c)


def test_identifiers_skip_strings_and_comments():
    ids = identifiers("SELECT `Order Items`.x FROM db.orders o -- users\n WHERE n = 'users' AND 1e5 > 0")
    assert "order items" in ids and "orders" in ids and "db" in ids
    assert "users" not in ids and "e5" not in ids


@pytest.mark.parametrize(
    "stmt,expected",
    [
        ("ALTER TABLE `users` RENAME COLUMN `name` TO `full_name`", [("altered", "users")]),
        ("ALTER TABLE `app`.`users` ADD COLUMN x int", [("altered", "users")]),
        ("DROP TABLE IF EXISTS `a`, b", [("dropped", "a"), ("dropped", "b")]),
        ("RENAME TABLE a TO b, `c` TO `d`", [("renamed", "a"), ("renamed", "b"), ("renamed", "c"), ("renamed", "d")]),
        ("CREATE TABLE IF NOT EXISTS `t` (id int)", [("created", "t")]),
        ("CREATE INDEX i ON t (x)", []),
    ],
)
def test_tables_touched(stmt, expected):
    assert tables_touched(stmt) == expected


# ---- view files -----------------------------------------------------------------------

def test_view_file_forms(tmp_path: Path):
    (tmp_path / "active_users.sql").write_text("-- only active\nSELECT id FROM users WHERE active = 1;\n")
    v = parse_view_file(tmp_path / "active_users.sql")
    assert v.name == "active_users"
    assert v.sql == "-- only active\nCREATE OR REPLACE VIEW `active_users` AS\nSELECT id FROM users WHERE active = 1"

    (tmp_path / "x.sql").write_text("CREATE ALGORITHM=MERGE SQL SECURITY INVOKER VIEW `db`.`Totals` AS SELECT 1;")
    v = parse_view_file(tmp_path / "x.sql")
    assert v.name == "Totals" and v.key == "totals"
    assert v.sql.startswith("CREATE OR REPLACE ALGORITHM=MERGE SQL SECURITY INVOKER VIEW")

    (tmp_path / "two.sql").write_text("SELECT 1; SELECT 2;")
    with pytest.raises(ProjectError, match="exactly one"):
        parse_view_file(tmp_path / "two.sql")

    (tmp_path / "bad.sql").write_text("UPDATE t SET x = 1")
    with pytest.raises(ProjectError, match="expected"):
        parse_view_file(tmp_path / "bad.sql")


# ---- planning -----------------------------------------------------------------------------

def mf(tmp_path: Path, name: str, text: str) -> MigrationFile:
    p = tmp_path / name
    p.write_text(text)
    return MigrationFile(p, text)


def views(tmp_path: Path, **defs: str):
    d = tmp_path / "views"
    d.mkdir(exist_ok=True)
    out = {}
    for name, body in defs.items():
        (d / f"{name}.sql").write_text(body)
        v = parse_view_file(d / f"{name}.sql")
        out[v.key] = v
    return out


BLOCK = """-- atlas-views:begin
-- view: a (new)
CREATE OR REPLACE VIEW `a` AS SELECT id, name FROM users;
-- view: b (new)
CREATE OR REPLACE VIEW `b` AS SELECT id FROM a;
-- atlas-views:end
"""


def test_first_sync_never_touches_existing_files(tmp_path):
    files = [mf(tmp_path, "001_init.sql", "CREATE TABLE users (id int);")]
    plan = plan_sync(files, views(tmp_path, a="SELECT id FROM users"))
    assert plan.target is None and [v.name for v, _ in plan.creates] == ["a"]


def test_appends_to_fresh_file_and_refreshes_dependents(tmp_path):
    files = [
        mf(tmp_path, "001_init.sql", "CREATE TABLE users (id int, name text);"),
        mf(tmp_path, "002_views.sql", BLOCK),
        mf(tmp_path, "003_x.sql", "-- Modify \"users\" table\nALTER TABLE `users` RENAME COLUMN `name` TO `full_name`;"),
    ]
    desired = views(tmp_path, a="SELECT id, name FROM users", b="SELECT id FROM a")
    plan = plan_sync(files, desired)
    assert plan.target.name == "003_x.sql"
    assert [(v.name, r.split(":")[0]) for v, r in plan.creates] == [("a", "refresh"), ("b", "refresh")]
    assert "table `users` altered in 003_x.sql" in plan.creates[0][1]
    assert len(plan.warnings) == 1 and "a, b" in plan.warnings[0]

    no_refresh = plan_sync(files, desired, refresh=False)
    assert no_refresh.creates == [] and len(no_refresh.warnings) == 2


def test_no_changes_still_marks_fresh_file(tmp_path):
    files = [
        mf(tmp_path, "001_init.sql", "CREATE TABLE users (id int, name text); CREATE TABLE t (x int);"),
        mf(tmp_path, "002_views.sql", BLOCK),
        mf(tmp_path, "003_x.sql", "ALTER TABLE t ADD COLUMN y int;"),
    ]
    plan = plan_sync(files, views(tmp_path, a="SELECT id, name FROM users", b="SELECT id FROM a"))
    assert plan.needs_write and not plan.has_view_changes
    assert "-- no view changes" in plan.block()


def test_in_sync_is_noop(tmp_path):
    files = [mf(tmp_path, "001_init.sql", "CREATE TABLE users (id int, name text);"), mf(tmp_path, "002_views.sql", BLOCK)]
    plan = plan_sync(files, views(tmp_path, a="SELECT id,\n  name FROM users -- same\n", b="SELECT id FROM a"))
    assert not plan.needs_write


def test_drop_and_topo_order(tmp_path):
    files = [mf(tmp_path, "001_init.sql", "CREATE TABLE users (id int, name text);"), mf(tmp_path, "002_views.sql", BLOCK)]
    # b now reads from c, which is new and must be created first; a is removed
    desired = views(tmp_path, b="SELECT id FROM c", c="SELECT id FROM users")
    plan = plan_sync(files, desired)
    assert plan.drops == [("a", "removed from views folder")]
    assert [v.name for v, _ in plan.creates] == ["c", "b"]
    block = plan.block()
    assert block.index("DROP VIEW") < block.index("VIEW `c`") < block.index("VIEW `b`")


def test_amend_rules(tmp_path):
    files = [
        mf(tmp_path, "001_init.sql", "CREATE TABLE users (id int, name text);"),
        mf(tmp_path, "002_views.sql", BLOCK),
    ]
    desired = views(tmp_path, a="SELECT id, name AS n FROM users", b="SELECT id FROM a")
    plan = plan_sync(files, desired, amend=True)
    assert plan.target.name == "002_views.sql"
    assert {v.name: r for v, r in plan.creates} == {"a": "new", "b": "new"}  # state before 002 had no views

    files.append(mf(tmp_path, "003_x.sql", "ALTER TABLE users ADD COLUMN z int;"))
    with pytest.raises(ProjectError, match="only rewrites the latest"):
        plan_sync(files, desired, amend=True)


def test_state_history(tmp_path):
    files = [
        mf(tmp_path, "002_views.sql", BLOCK),
        mf(tmp_path, "003_v.sql", "-- atlas-views:begin\nDROP VIEW IF EXISTS `b`;\nCREATE OR REPLACE VIEW `a` AS SELECT 1;\n-- atlas-views:end\n"),
    ]
    st = compute_state(files)
    assert list(st.views) == ["a"] and st.views["a"].source == "003_v.sql"
    assert st.history["b"] == [("002_views.sql", "create"), ("003_v.sql", "drop")]
    order, cycle = topo_order(compute_state(files[:1]).views)
    assert order == ["a", "b"] and not cycle


def test_custom_delimiter_target_is_refused(tmp_path):
    files = [
        mf(tmp_path, "002_views.sql", BLOCK),
        mf(tmp_path, "003_x.sql", "-- atlas:delimiter \\n\\n\nCREATE TABLE t (id int)\n\n"),
    ]
    with pytest.raises(ProjectError, match="delimiter"):
        plan_sync(files, {})


def test_parse_url():
    u = parse_url("mysql://me%40x:p%40ss%3Aw%2Frd@srv.mysql.database.azure.com:3306/ki_ds?tls=true")
    assert (u.user, u.password, u.host, u.port, u.database, u.tls) == (
        "me@x", "p@ss:w/rd", "srv.mysql.database.azure.com", 3306, "ki_ds", "true",
    )
    assert parse_url("mysql+pymysql://u:p@h/db").database == "db"
    with pytest.raises(ProjectError, match="database name"):
        parse_url("mysql://u:p@h:3306/")
