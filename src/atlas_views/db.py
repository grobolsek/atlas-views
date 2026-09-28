"""Database access: Atlas-style URL parsing, view queries, revisions table, throwaway MySQL."""

from __future__ import annotations

import secrets
import ssl
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .project import ProjectError
from .sqltext import quote_ident

SCHEMES = {"mysql", "maria", "mariadb"}


@dataclass
class DbUrl:
    raw: str  # as given (passed to atlas unchanged)
    host: str
    port: int
    user: str
    password: str
    database: str
    tls: str | None

    def redacted(self) -> str:
        return f"mysql://{self.user}:***@{self.host}:{self.port}/{self.database}"

    def connect(self, ssl_ca: str | None = None):
        import pymysql

        ctx = None
        tls = (self.tls or "").lower()
        if ssl_ca or tls in {"true", "1", "required", "verify-full", "verify_identity"}:
            ctx = ssl.create_default_context(cafile=ssl_ca)  # system trust store if no CA given
        elif tls in {"skip-verify", "preferred"}:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            return pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                ssl=ctx,
                autocommit=True,
                connect_timeout=10,
                charset="utf8mb4",
            )
        except pymysql.MySQLError as e:
            raise ProjectError(f"cannot connect to {self.redacted()}: {e.args[-1]}") from e


def parse_url(url: str) -> DbUrl:
    p = urlsplit(url)
    scheme = p.scheme.split("+", 1)[0]  # accept SQLAlchemy-style mysql+pymysql://
    if scheme not in SCHEMES:
        raise ProjectError(f"unsupported URL scheme {p.scheme!r}; expected mysql://user:pass@host:3306/db")
    db = p.path.lstrip("/")
    if not db:
        raise ProjectError("the URL must include the database name: mysql://user:pass@host:3306/<db>")
    q = parse_qs(p.query)
    return DbUrl(
        raw=url,
        host=p.hostname or "localhost",
        port=p.port or 3306,
        user=unquote(p.username or "root"),
        password=unquote(p.password or ""),
        database=unquote(db),
        tls=(q.get("tls") or [None])[0],
    )


def make_url(user: str, password: str, host: str, port: int, db: str) -> str:
    return f"mysql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{quote(db, safe='')}"


# ---- queries ----------------------------------------------------------------------


def list_views(conn, db: str) -> dict[str, str]:
    """Lowercased name -> actual name"""
    with conn.cursor() as cur:
        cur.execute("SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s", (db,))
        return {r[0].lower(): r[0] for r in cur.fetchall()}


def table_count(conn, db: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (db,))
        return int(cur.fetchone()[0])


def probe_view(conn, name: str) -> str | None:
    """None if `SELECT * FROM view LIMIT 0` works, else the error message."""
    import pymysql

    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {quote_ident(name)} LIMIT 0")
            cur.fetchall()
        return None
    except pymysql.MySQLError as e:
        return str(e.args[-1])


def execute(conn, sql: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)


@dataclass
class Revisions:
    applied: list[str]  # fully applied versions, ascending
    partial: str | None  # version that is only partially applied (failed), if any

    @property
    def current(self) -> str | None:
        return self.applied[-1] if self.applied else None


def read_revisions(conn, db: str) -> Revisions:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'atlas_schema_revisions'",
            (db,),
        )
        if not cur.fetchone()[0]:
            return Revisions([], None)
        cur.execute(f"SELECT version, applied, total FROM {quote_ident(db)}.atlas_schema_revisions")
        rows = [r for r in cur.fetchall() if not str(r[0]).startswith(".")]
    rows.sort(key=lambda r: str(r[0]))
    applied = [str(v) for v, a, t in rows if a >= t]
    partial = next((str(v) for v, a, t in rows if a < t), None)
    return Revisions(applied, partial)


# ---- throwaway MySQL in Docker -------------------------------------------------------


class Container:
    def __init__(self, image: str, mysqld_args: list[str], timeout: int = 120):
        try:
            import docker
        except ImportError as e:  # pragma: no cover
            raise ProjectError("the `docker` Python package is required for `check` (pip install docker)") from e
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as e:
            raise ProjectError(f"cannot reach Docker ({e}); start Docker or use --scratch-url") from e
        self.image = image
        self.mysqld_args = mysqld_args
        self.timeout = timeout
        self.password = secrets.token_hex(12)
        self.db = "atlas_views_check"
        self.container = None

    def start(self) -> str:
        """Start MySQL and wait until it accepts connections. Returns its URL."""
        import docker.errors
        import pymysql

        try:
            self.client.images.get(self.image)
        except docker.errors.ImageNotFound:
            print(f"pulling {self.image} ...", flush=True)
            self.client.images.pull(self.image)
        self.container = self.client.containers.run(
            self.image,
            command=self.mysqld_args or None,
            detach=True,
            remove=True,
            environment={"MYSQL_ROOT_PASSWORD": self.password, "MYSQL_DATABASE": self.db},
            ports={"3306/tcp": None},
            tmpfs={"/var/lib/mysql": ""},
            labels={"atlas-views": "check"},
        )
        deadline = time.monotonic() + self.timeout
        port = None
        while time.monotonic() < deadline:
            try:
                self.container.reload()
            except docker.errors.NotFound:
                raise ProjectError(f"{self.image} exited during startup")
            if self.container.status == "exited":
                logs = self.container.logs(tail=20).decode(errors="replace")
                raise ProjectError(f"{self.image} exited during startup:\n{logs}")
            bindings = (self.container.ports or {}).get("3306/tcp")
            if bindings:
                port = int(bindings[0]["HostPort"])
                try:
                    pymysql.connect(
                        host="127.0.0.1",
                        port=port,
                        user="root",
                        password=self.password,
                        database=self.db,
                        connect_timeout=2,
                    ).close()
                    return make_url("root", self.password, "127.0.0.1", port, self.db)
                except pymysql.MySQLError:
                    pass
            time.sleep(1)
        raise ProjectError(f"MySQL did not become ready within {self.timeout}s")

    def stop(self) -> None:
        if self.container is not None:
            try:
                self.container.stop(timeout=5)
            except Exception:
                pass
