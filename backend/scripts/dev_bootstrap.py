"""Zero-infrastructure local setup: `uv run python scripts/dev_bootstrap.py`

1. Starts (or reuses) a persistent embedded PostgreSQL 16 + pgvector in `backend/.pgdata`
   via `pgserver` (dev dependency; native on Windows/macOS/Linux, no Docker).
2. Creates the non-superuser application role `app` (so row-level security is enforced) and the
   `opportunity` database, then runs Alembic migrations as the owner.
3. Writes DATABASE_URL / DATABASE_MIGRATION_URL into `backend/.env`, generating ENCRYPTION_KEYS
   and JWT_SECRET if absent, and defaults REDIS_URL to `memory://` (single-process dev mode).

Re-run it after a reboot: the embedded server may come back on a different port.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import asyncpg
import pgserver

BACKEND = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND / ".pgdata"
ENV_FILE = BACKEND / ".env"
DB = "opportunity"
ROLE = "app"


def _set_env(lines: list[str], key: str, value: str, *, overwrite: bool) -> list[str]:
    pattern = re.compile(rf"^{re.escape(key)}=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            if overwrite:
                lines[i] = f"{key}={value}"
            return lines
    lines.append(f"{key}={value}")
    return lines


async def _prepare(admin_dsn: str, password: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE):
            await conn.execute(
                f"CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{password}'"
            )
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", DB):
            await conn.execute(f"CREATE DATABASE {DB}")
    finally:
        await conn.close()


async def _grant(db_dsn: str) -> None:
    conn = await asyncpg.connect(db_dsn)
    try:
        await conn.execute(
            f"""
            GRANT USAGE ON SCHEMA public TO {ROLE};
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE};
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {ROLE};
            """
        )
    finally:
        await conn.close()


PG_START_TIMEOUT_S = 300
PG_LOG = BACKEND.parent / ".logs" / "postgres.log"


def _start_postgres() -> None:
    """Start the embedded server ourselves, robustly, before pgserver attaches to it.

    pgserver gives `pg_ctl start` only 10 s and writes the server log INSIDE the data directory.
    After an unclean shutdown (PC switched off, process killed) Postgres runs crash recovery,
    which fsyncs the whole data directory - including that open log file, which Windows refuses
    ("sharing violation", retried for 30 s). Recovery then takes > 10 s, pgserver kills it, the
    data directory stays "interrupted", and every later launch fails the same way.

    Here: log OUTSIDE the data dir (no sharing violation) and wait up to 5 minutes, so recovery
    always completes. pgserver then finds the running server and reuses it."""
    from pgserver._commands import POSTGRES_BIN_PATH
    from pgserver.utils import PostmasterInfo, find_suitable_port

    info = PostmasterInfo.read_from_pgdata(DATA_DIR)
    if info is not None and info.is_running():
        return  # already up (pgserver reuses it)
    if not (DATA_DIR / "PG_VERSION").exists():
        return  # first run: let pgserver initdb and start it
    stale = DATA_DIR / "postmaster.pid"
    if stale.exists():
        stale.unlink()  # left by a crash; the process it names is gone (checked above)
    PG_LOG.parent.mkdir(parents=True, exist_ok=True)
    port = find_suitable_port("127.0.0.1")
    print(f"starting embedded PostgreSQL on 127.0.0.1:{port} (crash recovery may take a while)")
    subprocess.run(  # noqa: S603 - fixed binary path, no user input
        [
            str(POSTGRES_BIN_PATH / ("pg_ctl.exe" if os.name == "nt" else "pg_ctl")),
            "-D",
            str(DATA_DIR),
            "-w",
            "-t",
            str(PG_START_TIMEOUT_S),
            "-o",
            '-h "127.0.0.1"',
            "-o",
            f"-p {port}",
            "-l",
            str(PG_LOG),
            "start",
        ],
        check=True,
        timeout=PG_START_TIMEOUT_S + 30,
        # The postmaster outlives pg_ctl: never let it inherit our stdout/stderr (it would hold
        # the launcher's log file / a caller's pipe open forever). Its own log is PG_LOG.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("embedded PostgreSQL is up")


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    _start_postgres()
    server = pgserver.get_server(str(DATA_DIR), cleanup_mode=None)  # keep running after exit
    admin_uri: str = server.get_uri()
    base = admin_uri.rsplit("/", 1)[0]  # postgresql://postgres:@127.0.0.1:PORT
    host_port = base.split("@", 1)[1]

    password_file = DATA_DIR / ".app_password"
    if not password_file.exists():
        password_file.write_text(secrets.token_urlsafe(18))
    password = password_file.read_text().strip()

    owner_url = f"postgresql+asyncpg://postgres:@{host_port}/{DB}"
    app_url = f"postgresql+asyncpg://{ROLE}:{password}@{host_port}/{DB}"
    asyncio.run(_prepare(f"{base}/postgres", password))

    env = {**os.environ, "DATABASE_MIGRATION_URL": owner_url, "DATABASE_URL": owner_url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, check=True
    )
    asyncio.run(_grant(f"{base}/{DB}"))

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = _set_env(lines, "DATABASE_URL", app_url, overwrite=True)
    lines = _set_env(lines, "DATABASE_MIGRATION_URL", owner_url, overwrite=True)
    lines = _set_env(lines, "REDIS_URL", "memory://", overwrite=False)
    lines = _set_env(
        lines, "ENCRYPTION_KEYS", f"1:{base64.b64encode(os.urandom(32)).decode()}", overwrite=False
    )
    lines = _set_env(lines, "JWT_SECRET", secrets.token_urlsafe(48), overwrite=False)
    lines = _set_env(lines, "OUTBOX_RELAY_IN_API", "true", overwrite=False)
    # An empty ENCRYPTION_KEYS= line (copied from .env.example) must be filled in.
    lines = [
        f"ENCRYPTION_KEYS=1:{base64.b64encode(os.urandom(32)).decode()}"
        if line.strip() == "ENCRYPTION_KEYS="
        else line
        for line in lines
    ]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Embedded PostgreSQL running at {host_port} (data: {DATA_DIR})")
    print(f"Wrote {ENV_FILE}. Start the API with: uv run serve")


if __name__ == "__main__":
    main()
