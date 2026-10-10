"""Encrypted database backups for a copy outside Render (the owner's IT instruction, IT-5, 2026-10-10).

The owner's rule is 3-2-1: three copies, on two kinds of storage, one away
from the rest. For the bot's database:
  1. the live database (Render ``mgmg-db``);
  2. Render's own point-in-time recovery — 3 days (Hobby workspace) or 7
     (Pro), restored from the dashboard (Render → mgmg-db → Recovery);
  3. outside Render: a computer in the office asks for a backup every day
     (``scripts/backup/pull-backup.ps1``), keeps 30 days and 12 months, and
     copies each one to a second disk.

The file is ``pg_dump --format=custom`` of the whole database, encrypted
with GnuPG to the owner's PUBLIC key (``BACKUP_PUBLIC_KEY``). The server
never holds the private key: the file — on the office computer, a USB disk,
anywhere — can only be opened with the key in the owner's password vault.
So IT keeps and moves the files without being able to read them (the
Director's order of 07.10.2026: IT sees no company figures).

With it comes a manifest — when, size, SHA-256, and each table's row count
(counts only, never a value). The office script checks the SHA-256; the
restore test (``scripts/backup/restore-test.ps1``) restores the file into an
empty database and compares every table's count with the manifest.

Nothing here writes to the database: the counts are read in a READ ONLY
transaction and pg_dump only reads.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from integrations.common.config import settings
from integrations.common.db import fetch_read_only
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local

AGENT = "backup"
log = setup_logging(AGENT)

FOLDER = Path(tempfile.gettempdir()) / "mgmg-backup"
MIN_INTERVAL = 300          # seconds between two backups (one request can't be used to load the database)
KEEP_SECONDS = 3600         # a made file waits this long to be downloaded, then it's removed
TIMEOUT = 900               # pg_dump + gpg together, at most
FILE_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# Every table in the database with its exact row count, in one read-only query
# (``query_to_xml`` runs the count for each table the catalog lists — the
# names come from the catalog, never from a request).
COUNTS_SQL = """
SELECT t.table_name AS name,
       (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I.%I', t.table_schema, t.table_name),
                                            false, true, '')))[1]::text::bigint AS n
FROM information_schema.tables t
WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
ORDER BY t.table_name
"""


def public_key() -> str:
    """The owner's public key as set in Render; a value pasted on one line with \\n is accepted too."""
    text = (settings.backup_public_key or "").strip().replace("\\n", "\n")
    return text if "BEGIN PGP PUBLIC KEY BLOCK" in text else ""


def configured() -> bool:
    return bool(settings.backup_secret.get_secret_value().strip() and public_key() and settings.postgres_dsn)


def pg_dump_path() -> str | None:
    """pg_dump of the database's own major version (16) when installed there, else the one on PATH."""
    pinned = Path("/usr/lib/postgresql/16/bin/pg_dump")
    return str(pinned) if pinned.exists() else shutil.which("pg_dump")


def tools() -> dict[str, bool]:
    return {"pg_dump": pg_dump_path() is not None, "gpg": shutil.which("gpg") is not None}


def dump_argv(database_url: str) -> list[str]:
    """The whole database in pg_dump's own format (compressed, restorable table by table); never a shell."""
    return [pg_dump_path() or "pg_dump", "--format=custom", "--compress=6", "--no-owner", "--no-privileges",
            f"--dbname={database_url}"]


def gpg_argv(home: Path, key_file: Path, out: Path) -> list[str]:
    """Encrypt stdin to the owner's public key file — no keyring, no trust database, no private key."""
    return ["gpg", "--batch", "--no-tty", "--quiet", "--homedir", str(home), "--trust-model", "always",
            "--recipient-file", str(key_file), "--output", str(out), "--encrypt"]


def _scrub(text: str) -> str:
    """An error line for the log, with any connection string's password taken out."""
    text = re.sub(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@", r"\1***@", text or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:200]


@dataclass
class Backup:
    file_id: str
    path: Path
    created: datetime
    size: int
    sha256: str
    tables: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0

    def manifest(self) -> dict[str, Any]:
        """What the office keeps next to the file — times, size, hash and row counts only."""
        return {
            "file": self.file_id,
            "name": f"mgmg-db-{self.created:%Y-%m-%d-%H%M}.dump.gpg",
            "created": self.created.isoformat(timespec="seconds"),
            "format": "pg_dump custom (PostgreSQL 16), encrypted with GnuPG to the owner's backup key",
            "bytes": self.size,
            "sha256": self.sha256,
            "tables": self.tables,
            "rows": sum(self.tables.values()),
        }


_made: dict[str, Backup] = {}
_lock = asyncio.Lock()
_last_started = 0.0


def run_pipeline(dump_cmd: list[str], gpg_cmd: list[str], timeout: int = TIMEOUT) -> None:
    """pg_dump | gpg, each a plain process (no shell). Raises RuntimeError naming the step that failed."""
    with tempfile.TemporaryFile() as dump_err, tempfile.TemporaryFile() as gpg_err:
        dump = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=dump_err)
        try:
            enc = subprocess.Popen(gpg_cmd, stdin=dump.stdout, stdout=subprocess.DEVNULL, stderr=gpg_err)
        except Exception:
            dump.kill()
            raise
        assert dump.stdout is not None
        dump.stdout.close()  # gpg owns the pipe now; pg_dump gets SIGPIPE if gpg dies
        try:
            enc.wait(timeout=timeout)
            dump.wait(timeout=60)
        except subprocess.TimeoutExpired:
            dump.kill()
            enc.kill()
            raise RuntimeError(f"timed out after {timeout} s")
        if dump.returncode != 0:
            dump_err.seek(0)
            raise RuntimeError(f"pg_dump exit {dump.returncode}: {_scrub(dump_err.read().decode('utf-8', 'replace'))}")
        if enc.returncode != 0:
            gpg_err.seek(0)
            raise RuntimeError(f"gpg exit {enc.returncode}: {_scrub(gpg_err.read().decode('utf-8', 'replace'))}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cleanup(now: float | None = None) -> None:
    """Remove made files nobody fetched within KEEP_SECONDS (and strays from a restart)."""
    now = now or time.time()
    for file_id, backup in list(_made.items()):
        if now - backup.created.timestamp() > KEEP_SECONDS:
            backup.path.unlink(missing_ok=True)
            _made.pop(file_id, None)
    if FOLDER.exists():
        known = {b.path for b in _made.values()}
        for stray in FOLDER.glob("*.gpg"):
            if stray not in known and now - stray.stat().st_mtime > KEEP_SECONDS:
                stray.unlink(missing_ok=True)


class TooSoon(Exception):
    pass


async def make(*, dump_cmd: list[str] | None = None) -> Backup:
    """Dump, encrypt and describe the database; the file waits in FOLDER to be fetched.

    Raises:
        TooSoon: a backup was started less than MIN_INTERVAL seconds ago.
        RuntimeError: pg_dump or gpg failed (the reason, without secrets).
    """
    global _last_started
    async with _lock:
        if time.monotonic() - _last_started < MIN_INTERVAL and _last_started:
            raise TooSoon()
        _last_started = time.monotonic()
        cleanup()
        FOLDER.mkdir(parents=True, exist_ok=True)
        os.chmod(FOLDER, 0o700)
        started = time.monotonic()
        created = now_local()
        file_id = secrets.token_urlsafe(24)
        out = FOLDER / f"{file_id}.gpg"
        with tempfile.TemporaryDirectory() as home:
            key_file = Path(home) / "owner.asc"
            key_file.write_text(public_key(), encoding="utf-8")
            os.chmod(home, 0o700)
            command = dump_cmd or dump_argv(settings.postgres_dsn)
            await asyncio.to_thread(run_pipeline, command, gpg_argv(Path(home), key_file, out))
        rows = await fetch_read_only(COUNTS_SQL, timeout="60s")
        backup = Backup(file_id=file_id, path=out, created=created, size=out.stat().st_size,
                        sha256=await asyncio.to_thread(_sha256, out),
                        tables={r["name"]: int(r["n"]) for r in rows}, seconds=round(time.monotonic() - started, 1))
        _made[file_id] = backup
        return backup


def take(file_id: str) -> Backup | None:
    """A made file, by its id (from the manifest); None if unknown or already removed."""
    if not FILE_ID.match(file_id or ""):
        return None
    backup = _made.get(file_id)
    return backup if backup is not None and backup.path.exists() else None


def forget(file_id: str) -> None:
    """Delete a file once it has been fetched."""
    backup = _made.pop(file_id, None)
    if backup is not None:
        backup.path.unlink(missing_ok=True)


def status() -> dict[str, Any]:
    """For the office script's -Check: what is set and installed (no secrets, no data)."""
    return {"configured": configured(), "public_key": bool(public_key()), "tools": tools(),
            "keeps_minutes": KEEP_SECONDS // 60, "min_interval_minutes": MIN_INTERVAL // 60}


def manifest_json(backup: Backup) -> str:
    return json.dumps(backup.manifest(), ensure_ascii=False)
