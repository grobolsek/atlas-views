"""atlas-views: keep MySQL views in Atlas migrations (for Atlas without Pro)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .atlas import Atlas, dir_url
from .db import (
    Container,
    DbUrl,
    execute,
    list_views,
    parse_url,
    probe_view,
    read_revisions,
    table_count,
)
from .project import (
    MigrationFile,
    ProjectError,
    compute_state,
    files_upto,
    load_migrations,
    load_views,
    managed_views,
    topo_order,
)
from .sqltext import quote_ident
from .sync import plan_sync, write_plan

DEFAULTS = {
    "views-dir": "views",
    "migrations-dir": "migrations",
    "atlas": "atlas",
    "dev-url": None,
    "image": "mysql:8.0",
}
ENV = {
    "views-dir": "ATLAS_VIEWS_DIR",
    "migrations-dir": "ATLAS_VIEWS_MIGRATIONS_DIR",
    "atlas": "ATLAS_BIN",
    "dev-url": "ATLAS_DEV_URL",
    "image": "ATLAS_VIEWS_IMAGE",
}


def load_pyproject() -> dict:
    path = Path("pyproject.toml")
    if not path.exists():
        return {}
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        return {}
    with path.open("rb") as f:
        return tomllib.load(f).get("tool", {}).get("atlas-views", {})


def setting(args: argparse.Namespace, key: str, pyproject: dict) -> str | None:
    """Flag > environment variable > [tool.atlas-views] in pyproject.toml > default"""
    val = getattr(args, key.replace("-", "_"), None)
    if val:
        return val
    if os.environ.get(ENV[key]):
        return os.environ[ENV[key]]
    if pyproject.get(key):
        return str(pyproject[key])
    return DEFAULTS[key]


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


class Ctx:
    def __init__(self, args: argparse.Namespace):
        cfg = load_pyproject()
        self.args = args
        self.views_dir = Path(setting(args, "views-dir", cfg))
        self.migrations_dir = Path(setting(args, "migrations-dir", cfg))
        self.atlas = Atlas(setting(args, "atlas", cfg), self.migrations_dir)
        self.dev_url = setting(args, "dev-url", cfg)
        self.image = setting(args, "image", cfg)

    def db_url(self, required: bool = True) -> DbUrl | None:
        raw = getattr(self.args, "url", None) or os.environ.get("MYSQL_DATABASE_URL")
        if not raw:
            if required:
                raise ProjectError(
                    "no database URL: pass --url or set MYSQL_DATABASE_URL"
                )
            return None
        return parse_url(raw)

    def ssl_ca(self) -> str | None:
        return getattr(self.args, "ssl_ca", None) or os.environ.get("MYSQL_SSL_CA")


# ---- sync ---------------------------------------------------------------------------


def cmd_sync(ctx: Ctx) -> int:
    a = ctx.args
    files = load_migrations(ctx.migrations_dir)
    desired = load_views(ctx.views_dir)
    plan = plan_sync(
        files, desired, amend=a.amend, force_new=a.new, refresh=not a.no_refresh
    )

    if not plan.needs_write:
        print("Views are in sync with the migrations; nothing to do.")
        return 0

    if len(plan.fresh) > 1 and not a.amend:
        print(
            f"{len(plan.fresh)} migrations since the last sync: {', '.join(f.name for f in plan.fresh)}"
        )
    if plan.target is None:
        print(f"Target: new migration (`atlas migrate new {a.name}`)")
    else:
        print(
            f"Target: {plan.target.name} ({'rewriting its block' if a.amend else 'appending'})"
        )

    for w in plan.warnings:
        warn(w)
    for name, reason in plan.drops:
        print(f"  - {name}  ({reason})")
    for view, reason in plan.creates:
        mark = {"new": "+", "changed": "~"}.get(reason, "*")
        print(f"  {mark} {view.name}  ({reason})")
    if not plan.has_view_changes:
        print("  no view changes; marking the new migration(s) as processed")

    if a.dry_run:
        print("\n" + plan.block())
        return 0

    path = write_plan(plan, ctx.migrations_dir, ctx.atlas, a.name)
    print(f"Wrote {path} and updated atlas.sum")
    if plan.target is not None:
        print("Only edit migrations that have not been applied to any database yet.")
    return 0


# ---- state ----------------------------------------------------------------------------


def cmd_state(ctx: Ctx) -> int:
    a = ctx.args
    files = load_migrations(ctx.migrations_dir)
    if not files:
        print("No migrations yet.")
        return 0
    version = a.version or files[-1].version
    upto = files_upto(files, version)
    st = compute_state(upto)
    print(f"Views at version {version} ({upto[-1].name}):")
    if not st.views:
        print("  (none)")
    order, _ = topo_order(st.views)
    for k in order:
        v = st.views[k]
        hist = ", ".join(
            f"{act} {fname.split('_', 1)[0]}" for fname, act in st.history[k]
        )
        print(f"  {v.name:<32} {v.source}   [{hist}]")
        if a.sql:
            print("    " + v.sql.replace("\n", "\n    ") + ";\n")
    dropped = [k for k in st.history if k not in st.views]
    if dropped:
        print("Dropped: " + ", ".join(sorted(dropped)))

    if version == files[-1].version and ctx.views_dir.is_dir():
        desired = load_views(ctx.views_dir)
        plan = plan_sync(files, desired)
        if plan.has_view_changes:
            print("\nViews folder has unsynced changes (run `atlas-views sync`):")
            for name, _ in plan.drops:
                print(f"  - {name}")
            for v, reason in plan.creates:
                print(f"  {'+' if reason == 'new' else '~'} {v.name} ({reason})")
        elif plan.fresh:
            print(
                f"\n{len(plan.fresh)} migration(s) not processed yet: run `atlas-views sync`"
            )
    return 0


# ---- verification shared by check / reconcile -------------------------------------------


def verify(conn, db: str, files: list[MigrationFile], version: str | None) -> int:
    expected = compute_state(files_upto(files, version))
    managed = managed_views(files)
    actual = list_views(conn, db)
    problems = 0
    for k, v in expected.views.items():
        if k not in actual:
            print(f"  MISSING  {v.name}  (defined in {v.source})")
            problems += 1
    for k, name in sorted(actual.items()):
        if k in managed and k not in expected.views:
            print(f"  STALE    {name}  (should not exist at version {version})")
            problems += 1
    for k, name in sorted(actual.items()):
        err = probe_view(conn, name)
        tag = "" if k in managed else "  [not managed by atlas-views]"
        if err:
            print(f"  BROKEN   {name}: {err}{tag}")
            problems += 1
        else:
            print(f"  ok       {name}{tag}")
    if not actual and not expected.views:
        print("  (no views)")
    return problems


# ---- check ------------------------------------------------------------------------------


def cmd_check(ctx: Ctx) -> int:
    a = ctx.args
    files = load_migrations(ctx.migrations_dir)

    if a.url:  # read-only check of an existing database
        url = parse_url(a.url)
        conn = url.connect(ctx.ssl_ca())
        revs = read_revisions(conn, url.database)
        if revs.partial:
            warn(
                f"migration {revs.partial} is only partially applied on {url.redacted()}"
            )
        print(f"Checking views on {url.redacted()} (version {revs.current}):")
        problems = verify(conn, url.database, files, revs.current)
        conn.close()
        return report(problems)

    container = None
    try:
        if a.scratch_url:
            raw = a.scratch_url
        else:
            container = Container(ctx.image, a.mysqld_arg or [])
            print(f"Starting {ctx.image} ...", flush=True)
            raw = container.start()
        url = parse_url(raw)
        conn = url.connect()
        if a.scratch_url and table_count(conn, url.database):
            raise ProjectError(
                f"scratch database `{url.database}` is not empty; point --scratch-url at an empty one"
            )

        apply_args = [
            "migrate",
            "apply",
            "--dir",
            dir_url(ctx.migrations_dir),
            "--url",
            raw,
        ]
        if a.to_version:
            apply_args += ["--to-version", a.to_version]
        problems = 0
        try:
            ctx.atlas.run(*apply_args)
        except ProjectError as e:
            print(f"Migrations failed to apply:\n{e}")
            problems += 1

        revs = read_revisions(conn, url.database)
        version = revs.current
        if revs.partial:
            print(f"Migration {revs.partial} is only partially applied.")
        print(f"Applied migrations up to {version}. Views:")
        problems += verify(conn, url.database, files, version)

        if files and version == files[-1].version and not a.to_version:
            plan = plan_sync(files, load_views(ctx.views_dir))
            if plan.has_view_changes:
                names = [n for n, _ in plan.drops] + [v.name for v, _ in plan.creates]
                print(
                    f"  UNSYNCED views folder differs from migrations: {', '.join(names)} (run `atlas-views sync`)"
                )
                problems += 1
        conn.close()
        if container and a.keep:
            print(
                f"Container kept running: {url.redacted()}  (password: {url.password})"
            )
            container = None
        return report(problems)
    finally:
        if container:
            container.stop()


def report(problems: int) -> int:
    print("OK" if not problems else f"{problems} problem(s) found")
    return 1 if problems else 0


# ---- reconcile / down ---------------------------------------------------------------------


def reconcile(
    conn, db: str, files: list[MigrationFile], version: str | None, dry_run: bool
) -> int:
    target = compute_state(files_upto(files, version))
    managed = managed_views(files)
    actual = list_views(conn, db)
    order, _ = topo_order(target.views)
    drops = [
        name
        for k, name in sorted(actual.items())
        if k in managed and k not in target.views
    ]
    unmanaged = [name for k, name in sorted(actual.items()) if k not in managed]

    print(f"Reconciling views to version {version}:")
    for k in order:
        print(
            f"  create or replace {target.views[k].name}  (from {target.views[k].source})"
        )
    for name in drops:
        print(f"  drop {name}")
    if not order and not drops:
        print("  nothing to do")
    for name in unmanaged:
        warn(f"view `{name}` is not managed by atlas-views; left untouched")
    if dry_run:
        return 0

    for k in order:
        v = target.views[k]
        try:
            execute(conn, v.sql)
        except Exception as e:
            raise ProjectError(f"failed to create view `{v.name}`: {e}") from e
    for name in drops:
        execute(conn, f"DROP VIEW IF EXISTS {quote_ident(name)}")
    print("Done. Verifying:")
    return report(verify(conn, db, files, version))


def cmd_reconcile(ctx: Ctx) -> int:
    a = ctx.args
    files = load_migrations(ctx.migrations_dir)
    url = ctx.db_url()
    conn = url.connect(ctx.ssl_ca())
    version = a.version
    if not version:
        revs = read_revisions(conn, url.database)
        if revs.partial:
            warn(f"migration {revs.partial} is only partially applied")
        version = revs.current
    return reconcile(conn, url.database, files, version, a.dry_run)


def cmd_down(ctx: Ctx) -> int:
    a = ctx.args
    files = load_migrations(ctx.migrations_dir)
    url = ctx.db_url()
    conn = url.connect(ctx.ssl_ca())
    revs = read_revisions(conn, url.database)
    conn.close()
    if revs.partial:
        warn(f"migration {revs.partial} is only partially applied")
    if a.to_version:
        if a.to_version not in revs.applied:
            raise ProjectError(
                f"version {a.to_version} is not applied on {url.redacted()}"
            )
        target = a.to_version
        cmd = ["migrate", "down", "--to-version", a.to_version]
    else:
        n = a.amount
        if n < 1 or n > len(revs.applied):
            raise ProjectError(
                f"cannot revert {n} migration(s); {len(revs.applied)} applied"
            )
        target = revs.applied[-n - 1] if len(revs.applied) > n else None
        cmd = ["migrate", "down", str(n)]
    if a.env:
        cmd += ["--env", a.env]
    else:
        if not ctx.dev_url:
            raise ProjectError(
                "atlas migrate down needs a dev database: pass --dev-url, set ATLAS_DEV_URL, or use --env"
            )
        cmd += [
            "--url",
            url.raw,
            "--dir",
            dir_url(ctx.migrations_dir),
            "--dev-url",
            ctx.dev_url,
        ]
    if a.dry_run:
        cmd.append("--dry-run")
    cmd += a.atlas_args

    print(f"Reverting {url.redacted()}: {revs.current} -> {target}")
    ctx.atlas.run(*cmd, quiet=False)

    conn = url.connect(ctx.ssl_ca())
    if a.dry_run:
        return reconcile(conn, url.database, files, target, dry_run=True)
    now = read_revisions(conn, url.database).current
    if now != target:
        warn(
            f"database is at version {now}, expected {target}; reconciling views to {now}"
        )
    return reconcile(conn, url.database, files, now, dry_run=False)


# ---- argument parsing -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--views-dir", help="folder with one .sql file per view (default: views)"
    )
    common.add_argument(
        "--migrations-dir", help="Atlas migrations folder (default: migrations)"
    )
    common.add_argument("--atlas", help="atlas binary (default: atlas)")

    dbopts = argparse.ArgumentParser(add_help=False)
    dbopts.add_argument(
        "--url",
        help="database URL (default: $MYSQL_DATABASE_URL), e.g. mysql://user:pw@host:3306/db?tls=true",
    )
    dbopts.add_argument(
        "--ssl-ca",
        help="CA bundle for TLS (default: $MYSQL_SSL_CA, else the system trust store)",
    )

    p = argparse.ArgumentParser(prog="atlas-views", description=__doc__)
    p.add_argument("--version", action="version", version=f"atlas-views {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser(
        "sync", parents=[common], help="write view changes into the migrations"
    )
    s.add_argument(
        "--name", default="views", help="name for a new migration file (default: views)"
    )
    s.add_argument(
        "--new",
        action="store_true",
        help="always create a new migration instead of appending",
    )
    s.add_argument(
        "--amend",
        action="store_true",
        help="regenerate the block in the latest migration (only if not applied anywhere)",
    )
    s.add_argument(
        "--no-refresh",
        action="store_true",
        help="only warn about views affected by table changes; don't re-create them",
    )
    s.add_argument(
        "--dry-run",
        action="store_true",
        help="show the plan and the block without writing",
    )
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser(
        "state", parents=[common], help="show views as of a migration version"
    )
    s.add_argument(
        "--version", dest="version", help="migration version (default: latest)"
    )
    s.add_argument("--sql", action="store_true", help="print view definitions")
    s.set_defaults(func=cmd_state)

    s = sub.add_parser(
        "check",
        parents=[common],
        help="apply migrations to a throwaway MySQL and test every view",
    )
    s.add_argument(
        "--image", help="MySQL image; match your server version (default: mysql:8.0)"
    )
    s.add_argument(
        "--mysqld-arg",
        action="append",
        help="extra mysqld flag, e.g. --mysqld-arg=--lower-case-table-names=1",
    )
    s.add_argument("--to-version", help="apply migrations only up to this version")
    s.add_argument(
        "--scratch-url", help="use this empty database instead of starting Docker"
    )
    s.add_argument(
        "--url",
        help="read-only: check views on an existing database instead (no migrations applied)",
    )
    s.add_argument("--ssl-ca", help="CA bundle for TLS (default: $MYSQL_SSL_CA)")
    s.add_argument(
        "--keep", action="store_true", help="leave the container running afterwards"
    )
    s.set_defaults(func=cmd_check)

    s = sub.add_parser(
        "reconcile",
        parents=[common, dbopts],
        help="set the database's views to match its migration version",
    )
    s.add_argument(
        "--version",
        dest="version",
        help="version to reconcile to (default: read from atlas_schema_revisions)",
    )
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_reconcile)

    s = sub.add_parser(
        "down",
        parents=[common, dbopts],
        help="atlas migrate down, then reconcile views",
        description="Runs `atlas migrate down`, then restores views to the reverted version. Arguments after `--` are passed to atlas.",
    )
    s.add_argument(
        "amount",
        nargs="?",
        type=int,
        default=1,
        help="number of migrations to revert (default: 1)",
    )
    s.add_argument(
        "--to-version", help="revert down to this version (it stays applied)"
    )
    s.add_argument(
        "--env",
        help="atlas.hcl env to pass to atlas (its url must be the same database)",
    )
    s.add_argument(
        "--dev-url",
        help="dev database for atlas (default: $ATLAS_DEV_URL), e.g. docker://mysql/8/dev",
    )
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_down)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]
    args = build_parser().parse_args(argv)
    args.atlas_args = extra
    try:
        return args.func(Ctx(args))
    except ProjectError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
