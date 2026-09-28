#!/usr/bin/env python3
"""Stand-in for the atlas CLI in tests: migrate new | hash | apply | down.

Behaves like Atlas where it matters to atlas-views: `new` creates an empty
timestamped file, `hash` writes atlas.sum, `apply` refuses to run when
atlas.sum is stale and records atlas_schema_revisions, `down` removes
revisions (it does not revert table DDL).
"""

import base64
import datetime as dt
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from atlas_views.db import parse_url  # noqa: E402
from atlas_views.sqltext import split_statements  # noqa: E402


def flags(args):
    opts, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a in ("--dry-run",):
            opts[a] = True
        elif a.startswith("--"):
            opts[a] = args[i + 1]
            i += 1
        else:
            pos.append(a)
        i += 1
    return opts, pos


def sum_text(d: Path) -> str:
    files = sorted(d.glob("*.sql"))
    total = hashlib.sha256()
    lines = []
    for f in files:
        h = hashlib.sha256(f.name.encode() + f.read_bytes()).digest()
        total.update(h)
        lines.append(f"{f.name} h1:{base64.b64encode(h).decode()}")
    return "\n".join([f"h1:{base64.b64encode(total.digest()).decode()}", *lines]) + "\n"


def main():
    assert sys.argv[1] == "migrate", sys.argv
    sub, (opts, pos) = sys.argv[2], flags(sys.argv[3:])
    d = Path(opts["--dir"].removeprefix("file://")) if "--dir" in opts else Path("migrations")

    if sub == "hash":
        (d / "atlas.sum").write_text(sum_text(d))
        return 0

    if sub == "new":
        ts = dt.datetime.now(dt.timezone.utc)
        existing = {p.name.split("_")[0] for p in d.glob("*.sql")}
        if existing and max(existing) >= ts.strftime("%Y%m%d%H%M%S"):
            ts = dt.datetime.strptime(max(existing), "%Y%m%d%H%M%S") + dt.timedelta(seconds=1)
        name = pos[0] if pos else ""
        (d / f"{ts.strftime('%Y%m%d%H%M%S')}{'_' + name if name else ''}.sql").write_text("")
        (d / "atlas.sum").write_text(sum_text(d))
        return 0

    url = parse_url(opts["--url"])
    conn = url.connect()
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS atlas_schema_revisions (version varchar(255) PRIMARY KEY, "
        "applied int NOT NULL, total int NOT NULL, error text)"
    )
    cur.execute("SELECT version FROM atlas_schema_revisions ORDER BY version")
    applied = [r[0] for r in cur.fetchall()]

    if sub == "apply":
        sum_path = d / "atlas.sum"
        if not sum_path.exists() or sum_path.read_text() != sum_text(d):
            print("Error: checksum mismatch", file=sys.stderr)
            return 1
        to = opts.get("--to-version")
        for f in sorted(d.glob("*.sql")):
            v = f.name.split("_")[0].removesuffix(".sql")
            if v in applied:
                continue
            if to and v > to:
                break
            stmts = split_statements(f.read_text())
            for i, st in enumerate(stmts):
                try:
                    cur.execute(st.code)
                except Exception as e:
                    cur.execute(
                        "REPLACE INTO atlas_schema_revisions VALUES (%s,%s,%s,%s)", (v, i, len(stmts), str(e))
                    )
                    print(f"Error: executing statement {i + 1} of {f.name}: {e}", file=sys.stderr)
                    return 1
            cur.execute("REPLACE INTO atlas_schema_revisions VALUES (%s,%s,%s,NULL)", (v, len(stmts), len(stmts)))
        return 0

    if sub == "down":
        if "--dev-url" not in opts and "--env" not in opts:
            print("Error: dev-url required", file=sys.stderr)
            return 1
        if "--to-version" in opts:
            revert = [v for v in applied if v > opts["--to-version"]]
        else:
            n = int(pos[0]) if pos else 1
            revert = applied[-n:]
        print(f"fake atlas: reverting {revert}{' (dry run)' if '--dry-run' in opts else ''}")
        if "--dry-run" not in opts:
            for v in revert:
                cur.execute("DELETE FROM atlas_schema_revisions WHERE version = %s", (v,))
        return 0

    print(f"unsupported: {sys.argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
