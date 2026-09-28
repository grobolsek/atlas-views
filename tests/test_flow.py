"""End-to-end: CLI + fake atlas + a real MySQL/MariaDB server.

Needs a server reachable at $ATLAS_VIEWS_TEST_URL (a user allowed to create
databases), e.g. mysql://root:pw@127.0.0.1:3306/ignored
"""

import os
import sys
import uuid
from pathlib import Path

import pytest

from atlas_views.cli import main
from atlas_views.db import make_url, parse_url

FAKE_ATLAS = str(Path(__file__).with_name("fake_atlas.py"))
BASE = os.environ.get("ATLAS_VIEWS_TEST_URL")
pytestmark = pytest.mark.skipif(not BASE, reason="set ATLAS_VIEWS_TEST_URL to run end-to-end tests")


@pytest.fixture
def server():
    base = parse_url(BASE)
    admin = base.connect()
    created = []

    def new_db() -> str:
        name = f"av_{uuid.uuid4().hex[:8]}"
        admin.cursor().execute(f"CREATE DATABASE `{name}`")
        created.append(name)
        return make_url(base.user, base.password, base.host, base.port, name)

    yield new_db
    for name in created:
        admin.cursor().execute(f"DROP DATABASE `{name}`")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATLAS_BIN", FAKE_ATLAS)
    monkeypatch.setenv("ATLAS_DEV_URL", "docker://mysql/8/dev")
    (tmp_path / "views").mkdir()
    (tmp_path / "migrations").mkdir()
    return tmp_path


def atlas_diff(project: Path, version: str, name: str, sql: str) -> None:
    """Simulate `atlas migrate diff`: a new file plus a fresh atlas.sum."""
    (project / "migrations" / f"{version}_{name}.sql").write_text(sql)
    assert os.system(f"{sys.executable} {FAKE_ATLAS} migrate hash --dir file://migrations") == 0


def run(*args) -> int:
    return main(list(args))


def views_in(url: str) -> set[str]:
    u = parse_url(url)
    c = u.connect()
    cur = c.cursor()
    cur.execute("SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s", (u.database,))
    return {r[0] for r in cur.fetchall()}


def test_full_flow(project, server, capsys):
    atlas_diff(
        project,
        "20260901000000",
        "init",
        "-- Create \"users\" table\nCREATE TABLE `users` (`id` int NOT NULL, `name` varchar(50), `active` tinyint, PRIMARY KEY (`id`));\n"
        "-- Create \"orders\" table\nCREATE TABLE `orders` (`id` int NOT NULL, `user_id` int, `total` decimal(10,2), PRIMARY KEY (`id`));\n",
    )
    V = project / "views"
    (V / "active_users.sql").write_text("-- users with active = 1\nSELECT id, name FROM users WHERE active = 1;\n")
    (V / "user_totals.sql").write_text(
        "CREATE VIEW user_totals AS\nSELECT u.id, u.name, SUM(o.total) AS total\nFROM active_users u JOIN orders o ON o.user_id = u.id\nGROUP BY u.id, u.name;\n"
    )
    (V / "order_count.sql").write_text("CREATE ALGORITHM=MERGE VIEW `order_count` AS SELECT COUNT(*) AS n FROM orders;")

    # 1. first sync: new migration via `atlas migrate new`, dependency order respected
    assert run("sync") == 0
    files = sorted((project / "migrations").glob("*.sql"))
    assert len(files) == 2 and files[1].name.endswith("_views.sql")
    body = files[1].read_text()
    assert body.index("`active_users`") < body.index("VIEW user_totals")
    assert run("sync") == 0 and "nothing to do" in capsys.readouterr().out

    # 2. check on a scratch DB: all good
    assert run("check", "--scratch-url", server()) == 0
    assert capsys.readouterr().out.rstrip().endswith("OK")

    # 3. Atlas renames a column the views rely on -> sync appends refreshes to that file
    atlas_diff(project, "20990101000000", "rename", "-- Modify \"users\" table\nALTER TABLE `users` RENAME COLUMN `name` TO `full_name`;\n")
    assert run("sync") == 0
    out = capsys.readouterr()
    assert "appending" in out.out and "* active_users  (refresh: table `users` altered" in out.out
    assert "2 view(s) depend on changed tables/views" in out.err
    rename = (project / "migrations" / "20990101000000_rename.sql").read_text()
    assert rename.startswith("-- Modify") and "refresh" in rename and "order_count" not in rename

    # 4. check catches the broken view at migration time
    assert run("check", "--scratch-url", server()) == 1
    out = capsys.readouterr().out
    assert "Migrations failed to apply" in out and "is only partially applied" in out
    assert "p%40ss" not in out and ":***@" in out  # password redacted

    # 5. fix the view, regenerate the block in place
    (V / "active_users.sql").write_text("SELECT id, full_name AS name FROM users WHERE active = 1;\n")
    assert run("sync", "--amend") == 0
    rename = (project / "migrations" / "20990101000000_rename.sql").read_text()
    assert rename.count("atlas-views:begin") == 1 and "active_users (changed)" in rename
    assert "user_totals (refresh: depends on view `active_users`, which is changed)" in rename
    assert run("check", "--scratch-url", server()) == 0

    # 6. remove a view -> DROP in a new migration; state shows history
    (V / "order_count.sql").unlink()
    assert run("sync") == 0
    last = sorted((project / "migrations").glob("*.sql"))[-1]
    assert "DROP VIEW IF EXISTS `order_count`" in last.read_text()
    capsys.readouterr()
    assert run("state") == 0
    out = capsys.readouterr().out
    assert "Dropped: order_count" in out and "replace 20990101000000" in out

    # 7. "prod": apply everything, then go down one step -> order_count is back
    prod = server()
    assert os.system(f"{sys.executable} {FAKE_ATLAS} migrate apply --dir file://migrations --url '{prod}'") == 0
    assert views_in(prod) == {"active_users", "user_totals"}
    assert run("check", "--url", prod) == 0
    assert run("down", "--url", prod) == 0
    assert views_in(prod) == {"active_users", "user_totals", "order_count"}

    # 8. down to the initial version -> managed views removed; unmanaged ones untouched
    c = parse_url(prod).connect()
    c.cursor().execute("CREATE VIEW handmade AS SELECT 1 AS x")
    assert run("down", "--url", prod, "--to-version", "20260901000000") == 0
    assert views_in(prod) == {"handmade"}
    assert "not managed by atlas-views" in capsys.readouterr().err

    # 9. reconcile is idempotent
    assert run("reconcile", "--url", prod) == 0


def test_sync_rolls_back_when_atlas_fails(project, monkeypatch):
    atlas_diff(project, "20260901000000", "init", "CREATE TABLE t (id int);\n")
    (project / "views" / "v.sql").write_text("SELECT id FROM t")
    assert run("sync") == 0
    atlas_diff(project, "20990101000000", "x", "ALTER TABLE t ADD COLUMN y int;\n")
    before = {p.name: p.read_bytes() for p in (project / "migrations").iterdir()}
    (project / "views" / "v.sql").write_text("SELECT id, y FROM t")
    monkeypatch.setenv("ATLAS_BIN", "false")  # every atlas call fails
    assert run("sync") == 2
    after = {p.name: p.read_bytes() for p in (project / "migrations").iterdir()}
    assert before == after
