"""Thin wrapper around the atlas CLI."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .project import ProjectError


def dir_url(path: Path) -> str:
    return "file://" + path.as_posix()


def redact(text: str) -> str:
    """Hide passwords in URLs (scheme://user:password@host)."""
    return re.sub(r"(://[^:/@\s]*):[^@\s]*@", r"\1:***@", text)


class Atlas:
    def __init__(self, binary: str, migrations_dir: Path):
        self.binary = binary
        self.migrations_dir = migrations_dir

    def _bin(self) -> str:
        found = shutil.which(self.binary)
        if not found:
            raise ProjectError(f"atlas binary not found: {self.binary!r} (set --atlas or ATLAS_BIN)")
        return found

    def run(self, *args: str, quiet: bool = True) -> None:
        cmd = [self._bin(), *args]
        if quiet:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                out = (res.stderr or res.stdout).strip()
                raise ProjectError(redact(f"`atlas {' '.join(args)}` failed:\n{out}"))
        else:
            res = subprocess.run(cmd)
            if res.returncode != 0:
                raise ProjectError(f"`atlas {args[0]} {args[1] if len(args) > 1 else ''}` exited with {res.returncode}")

    def migrate(self, sub: str, *args: str, quiet: bool = True) -> None:
        self.run("migrate", sub, *args, "--dir", dir_url(self.migrations_dir), quiet=quiet)
