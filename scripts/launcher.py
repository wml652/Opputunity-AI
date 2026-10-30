"""OPPORTUNITIES ON THE GO: one-click launcher and process supervisor (stdlib only).

    python scripts/launcher.py            start everything (idempotent) and open the browser
    python scripts/launcher.py --stop     stop the API and frontend
    python scripts/launcher.py --status   print what is running
    python scripts/launcher.py --install  (re)create the Desktop / repo shortcuts and icon

Boot sequence: configure PATH for uv/pnpm -> if the API and console already answer, just open
the browser -> otherwise ensure the embedded PostgreSQL + migrations + dev settings
(scripts/dev_bootstrap.py), start the API and Next.js dev server as detached background
processes logging to .logs/, wait for both to return HTTP 200, self-heal a corrupted Next.js
dev cache once, then open http://localhost:3000.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
LOGS = ROOT / ".logs"
PIDS = LOGS / "pids.json"

API_HEALTH = "http://127.0.0.1:8000/api/v1/health"
CONSOLE = "http://localhost:3000"
API_PORT, WEB_PORT = 8000, 3000
IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------------------------
# output


def say(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    LOGS.mkdir(exist_ok=True)
    with (LOGS / "launcher.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


class LaunchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------
# environment


def tool_dirs() -> list[Path]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    dirs = [appdata / "npm", home / ".local" / "bin", home / ".cargo" / "bin"]
    dirs += sorted(appdata.glob("Python/Python3*/Scripts"), reverse=True)  # pip --user installs
    dirs += [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs"]
    return [d for d in dirs if d.is_dir()]


def build_env() -> dict[str, str]:
    env = dict(os.environ)
    extra = os.pathsep.join(str(d) for d in tool_dirs())
    env["PATH"] = extra + os.pathsep + env.get("PATH", "")
    env["NEXT_TELEMETRY_DISABLED"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def which(name: str, env: dict[str, str]) -> str:
    found = shutil.which(name, path=env["PATH"])
    if not found:
        raise LaunchError(f"'{name}' was not found. Install it, or add it to PATH.")
    return found


# ---------------------------------------------------------------------------------------------
# probes


def http_status(url: str, timeout: float = 3.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost only
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def http_text(url: str, timeout: float = 30.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost only
        return resp.read().decode("utf-8", "replace")


def api_up() -> bool:
    return http_status(API_HEALTH) == 200


def console_up() -> bool:
    return http_status(CONSOLE, timeout=60) == 200


def console_assets_ok() -> bool:
    """A stale/corrupted Next.js cache serves HTML whose core chunks 404 (page never hydrates)."""
    try:
        html = http_text(CONSOLE, timeout=90)
    except OSError:
        return False
    chunks = re.findall(r'src="(/_next/static/chunks/[^"]+)"', html)
    if not chunks:
        return False
    return all(http_status(CONSOLE + c, timeout=60) == 200 for c in chunks[:4])


def pids_on_port(port: int) -> set[int]:
    pids: set[int] = set()
    if IS_WINDOWS:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, check=False
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                pids.add(int(parts[4]))
    else:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True,
            check=False,
        ).stdout
        pids.update(int(p) for p in out.split() if p.isdigit())
    pids.discard(0)
    return pids


# ---------------------------------------------------------------------------------------------
# process management


def spawn(name: str, args: list[str], cwd: Path, env: dict[str, str]) -> int:
    LOGS.mkdir(exist_ok=True)
    log = (LOGS / f"{name}.log").open("ab")
    log.write(f"\n===== {time.ctime()} starting {name}: {' '.join(args)}\n".encode())
    log.flush()
    kwargs: dict[str, object] = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        args, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, **kwargs,  # type: ignore[arg-type]
    )
    record_pid(name, proc.pid)
    return proc.pid


def record_pid(name: str, pid: int) -> None:
    data = load_pids()
    data[name] = pid
    LOGS.mkdir(exist_ok=True)
    PIDS.write_text(json.dumps(data, indent=2))


def load_pids() -> dict[str, int]:
    try:
        return {k: int(v) for k, v in json.loads(PIDS.read_text()).items()}
    except (OSError, ValueError):
        return {}


def kill_tree(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False
        )
    else:
        try:
            os.killpg(pid, 15)
        except OSError:
            pass


def stop_service(name: str, port: int) -> bool:
    targets = set(pids_on_port(port))
    recorded = load_pids().get(name)
    if recorded:
        targets.add(recorded)
    for pid in targets:
        kill_tree(pid)
    deadline = time.time() + 15
    while pids_on_port(port) and time.time() < deadline:
        time.sleep(0.3)
    data = load_pids()
    data.pop(name, None)
    if LOGS.exists():
        PIDS.write_text(json.dumps(data, indent=2))
    return bool(targets)


def wait_for(check, label: str, timeout: float) -> None:  # type: ignore[no-untyped-def]
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        if check():
            say(f"{label} is up")
            return
        time.sleep(1.5)
        dots += 1
        if dots % 10 == 0:
            say(f"waiting for {label}...")
    raise LaunchError(f"{label} did not come up within {int(timeout)}s (see .logs/)")


# ---------------------------------------------------------------------------------------------
# steps


def bootstrap(env: dict[str, str]) -> None:
    uv = which("uv", env)
    if not (BACKEND / ".venv").exists() or not (BACKEND / "uv.lock").exists():
        say("installing backend dependencies (first run, can take a while)")
    say("syncing backend dependencies")
    run([uv, "sync", "--frozen"], BACKEND, env, "uv-sync")
    say("ensuring embedded PostgreSQL, migrations, and dev settings")
    run([uv, "run", "--frozen", "python", "scripts/dev_bootstrap.py"], BACKEND, env, "bootstrap")
    ensure_env_flag(BACKEND / ".env", "DEV_AUTH_BYPASS", "true")


def run(args: list[str], cwd: Path, env: dict[str, str], name: str) -> None:
    LOGS.mkdir(exist_ok=True)
    with (LOGS / f"{name}.log").open("ab") as log:
        log.write(f"\n===== {time.ctime()} {' '.join(args)}\n".encode())
        log.flush()
        result = subprocess.run(  # noqa: S603 - fixed argv
            args, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
        )
    if result.returncode != 0:
        raise LaunchError(f"{name} failed (exit {result.returncode}); see .logs/{name}.log")


def ensure_env_flag(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, found = [], False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def start_api(env: dict[str, str]) -> None:
    if api_up():
        say("API already running on :8000")
        return
    if pids_on_port(API_PORT):
        say("port 8000 is held by an unresponsive process; restarting it")
        stop_service("api", API_PORT)
    say("starting API (FastAPI) on :8000")
    spawn("api", [which("uv", env), "run", "--frozen", "serve"], BACKEND, env)
    wait_for(api_up, "API", timeout=120)


def ensure_frontend_deps(env: dict[str, str]) -> None:
    if not (FRONTEND / "node_modules").exists():
        say("installing frontend dependencies (first run, can take a while)")
        run([which("pnpm", env), "install", "--frozen-lockfile"], FRONTEND, env, "pnpm-install")


def start_frontend(env: dict[str, str], *, attempt: int = 1) -> None:
    if console_up() and console_assets_ok():
        say("console already running on :3000")
        return
    if pids_on_port(WEB_PORT):
        say("port 3000 is held by a stale console; restarting it")
        stop_service("web", WEB_PORT)
    if attempt > 1:
        say("clearing the Next.js dev cache (.next-dev)")
        shutil.rmtree(FRONTEND / ".next-dev", ignore_errors=True)
    say("starting console (Next.js) on :3000 - first compile takes ~30-60s")
    spawn("web", [which("pnpm", env), "dev"], FRONTEND, env)
    wait_for(console_up, "console", timeout=240)
    if not console_assets_ok():
        if attempt >= 2:
            raise LaunchError("console assets still failing after a cache reset; see .logs/web.log")
        say("console assets are broken (stale cache); self-healing")
        stop_service("web", WEB_PORT)
        start_frontend(env, attempt=attempt + 1)


def open_browser() -> None:
    say(f"opening {CONSOLE}")
    webbrowser.open(CONSOLE)


class _LaunchLock:
    """One launch at a time: a double-click must not run two bootstraps against the same
    embedded database (they killed each other's PostgreSQL startup)."""

    def __init__(self) -> None:
        LOGS.mkdir(exist_ok=True)
        self._file = (LOGS / "launcher.lock").open("a+b")

    def __enter__(self) -> bool:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def __exit__(self, *_exc: object) -> None:
        self._file.close()  # releases the lock


def start() -> None:
    with _LaunchLock() as acquired:
        if not acquired:
            say("a launch is already in progress; the browser opens when it is ready")
            return
        _start()


def _start() -> None:
    env = build_env()
    if api_up() and console_up():
        say("OPPORTUNITIES ON THE GO is already running")
        open_browser()
        return
    bootstrap(env)
    start_api(env)
    ensure_frontend_deps(env)
    start_frontend(env)
    open_browser()
    say("ready. Stop everything with 'Stop Mission Control'")


def stop() -> None:
    stopped_web = stop_service("web", WEB_PORT)
    stopped_api = stop_service("api", API_PORT)
    say(f"console: {'stopped' if stopped_web else 'not running'}")
    say(f"API: {'stopped' if stopped_api else 'not running'}")
    say("the embedded PostgreSQL keeps running in the background (it is reused on next launch)")


def status() -> None:
    print(f"API      {API_HEALTH}: {'UP' if api_up() else 'down'}")
    print(f"console  {CONSOLE}: {'UP' if console_up() else 'down'}")
    print(f"pids     {load_pids()}")


# ---------------------------------------------------------------------------------------------
# shortcuts + icon


def install_shortcuts() -> None:
    from make_icon import write_icon  # local module (scripts/make_icon.py)

    icon = ROOT / "scripts" / "assets" / "mission-control.ico"
    write_icon(icon)
    target = ROOT / "Launch Mission Control.cmd"
    desktop = desktop_dir()
    for folder in (desktop, ROOT):
        legacy = folder / "Opportunity Mission Control.lnk"  # pre-rebrand shortcut name
        if legacy.exists():
            legacy.unlink()
        link = folder / "Opportunities On The Go.lnk"
        create_shortcut(link, target, icon, "Start OPPORTUNITIES ON THE GO")
        say(f"shortcut: {link}")
    stop_link = ROOT / "Stop Mission Control.lnk"
    create_shortcut(stop_link, ROOT / "Stop Mission Control.cmd", icon, "Stop OPPORTUNITIES ON THE GO")


def desktop_dir() -> Path:
    if IS_WINDOWS:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if out:
            return Path(out)
    return Path.home() / "Desktop"


def create_shortcut(link: Path, target: Path, icon: Path, description: str) -> None:
    if not IS_WINDOWS:
        return
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK_PATH);"
        "$s.TargetPath = $env:LNK_TARGET; $s.WorkingDirectory = $env:LNK_CWD;"
        "$s.IconLocation = $env:LNK_ICON + ',0'; $s.Description = $env:LNK_DESC;"
        "$s.WindowStyle = 7; $s.Save()"
    )
    env = dict(os.environ)
    env.update(
        LNK_PATH=str(link), LNK_TARGET=str(target), LNK_CWD=str(ROOT), LNK_ICON=str(icon),
        LNK_DESC=description,
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        env=env, check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="OPPORTUNITIES ON THE GO launcher")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stop", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--install", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        if args.stop:
            stop()
        elif args.status:
            status()
        elif args.install:
            install_shortcuts()
        else:
            start()
    except LaunchError as exc:
        say(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
