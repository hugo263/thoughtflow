#!/usr/bin/env python3
"""Start or reuse the local Thoughtflow service without exporting credentials."""

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

try:
    import fcntl
except ImportError:
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.paths import runtime_dir
RUNTIME = runtime_dir()
STATE = RUNTIME / "server.json"
LOG = RUNTIME / "server.log"
URL_PATTERN = re.compile(r"http://127\.0\.0\.1:(\d{1,5})/")
THREAD_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")


def get_health(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=1) as response:
            if response.status != 200:
                return None
            return json.load(response)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, PermissionError):
            raise RuntimeError("Cannot reach the local service. This environment may restrict local network access.") from None
        return None
    except PermissionError:
        raise RuntimeError("Cannot reach the local service. This environment may restrict local network access.") from None
    except (OSError, ValueError):
        return None


def existing_service():
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        port, pid = int(state["port"]), int(state["pid"])
        health = get_health(port)
        if health and health.get("ok") is True and health.get("service") == "thought-flow" and health.get("pid") == pid:
            return state
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def save_state(state):
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(str(temp), str(STATE))


@contextlib.contextmanager
def launch_lock():
    """Serialize simultaneous launches from multiple Codex chats."""
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(RUNTIME / "launch.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        if fcntl:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        elif os.name == "nt":
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            elif os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def start_service(requested_port=None):
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(RUNTIME, 0o700)
    except OSError:
        pass
    existing = existing_service()
    if existing:
        if requested_port not in (None, 0, existing["port"]):
            raise RuntimeError("The service is already running on port %s. Stop it before choosing another port." % existing["port"])
        return existing
    port = requested_port
    if port is None:
        try:
            port = int(json.loads(STATE.read_text(encoding="utf-8"))["port"])
            if not 0 <= port <= 65535:
                port = 0
        except (OSError, ValueError, KeyError, TypeError):
            port = 0
    server = ROOT / "backend" / "server.py"
    if not server.is_file():
        raise RuntimeError("Map service not found: %s" % server)
    fd = os.open(str(LOG), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-u", str(server), "--port", str(port)],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("The local service failed to start. The previous port may be in use. Check %s" % LOG)
        try:
            launch_output = LOG.read_text(encoding="utf-8", errors="replace")
            match = URL_PATTERN.search(launch_output)
            if match:
                port = int(match.group(1))
                health = get_health(port)
                if health and health.get("ok") is True and health.get("service") == "thought-flow" and health.get("pid") == process.pid:
                    state = {"pid": process.pid, "port": port}
                    save_state(state)
                    return state
        except OSError:
            pass
        time.sleep(0.1)
    process.terminate()
    raise RuntimeError("The local service was not ready within 15 seconds. Check %s" % LOG)


def main():
    parser = argparse.ArgumentParser(description="Start or stop the local Thoughtflow service")
    parser.add_argument("--thread", help="Optional conversation ID; defaults to the current CODEX_THREAD_ID")
    parser.add_argument("--open", action="store_true", help="Open the map in your default browser")
    parser.add_argument("--stop", action="store_true", help="Stop the local service started by this launcher")
    parser.add_argument("--port", type=int, help="Local port; defaults to the previous port, or 0 to choose automatically")
    args = parser.parse_args()
    if args.port is not None and not 0 <= args.port <= 65535:
        raise RuntimeError("The port must be between 0 and 65535.")
    if args.stop and args.port is not None:
        raise RuntimeError("Do not specify a port when stopping the service.")
    thread = args.thread or os.environ.get("CODEX_THREAD_ID", "")
    if not args.stop and thread and not THREAD_PATTERN.fullmatch(thread):
        raise RuntimeError("Invalid conversation ID format.")
    with launch_lock():
        if args.stop:
            state = existing_service()
            if not state:
                print("The local service is not running.")
                return
            os.kill(state["pid"], signal.SIGTERM)
            for _ in range(30):
                if not get_health(state["port"]):
                    break
                time.sleep(.1)
            save_state({"pid": 0, "port": state["port"]})
            print("Local service stopped. Restarting will prefer the same port.")
            return
        state = start_service(args.port)
    url = "http://127.0.0.1:%d/" % state["port"]
    if thread:
        url += "?" + urlencode({"thread": thread})
    print(url, flush=True)
    if args.open:
        webbrowser.open(url)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
