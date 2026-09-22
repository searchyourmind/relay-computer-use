"""Supervise the private synthetic bank and public replay console.

Only the console listens publicly. If either child fails, stop both and let the
container platform restart a consistent application. Discovery can use a private
model service configured through OLLAMA_HOST.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STOP = threading.Event()
CHILDREN: list[tuple[str, subprocess.Popen]] = []


def request_stop(signum, frame):
    STOP.set()


def spawn(name: str, command: list[str]):
    env = os.environ.copy()
    child = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
    CHILDREN.append((name, child))
    print(f"relay: started {name}", flush=True)
    return child


def check_children():
    if STOP.is_set():
        raise InterruptedError("shutdown requested")
    for name, child in CHILDREN:
        if child.poll() is not None:
            raise RuntimeError(f"{name} stopped unexpectedly (exit {child.returncode})")


def wait_ready(url: str, timeout: int = 60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_children()
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        STOP.wait(0.25)
    raise RuntimeError("synthetic bank did not become ready")


def shutdown():
    # The console must get a chance to abort runs and close browsers first.
    for _, child in reversed(CHILDREN):
        if child.poll() is not None:
            continue
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=12)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        except ProcessLookupError:
            pass


def main():
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        spawn("synthetic bank", [sys.executable, "-m", "uvicorn", "relaycu.demo:app",
                                 "--host", "127.0.0.1", "--port", "4311", "--no-access-log"])
        wait_ready("http://127.0.0.1:4311/")
        check_children()
        port = str(int(os.getenv("PORT", "8080")))
        spawn("console", [sys.executable, "-m", "uvicorn", "relaycu.server:app",
                          "--host", "0.0.0.0", "--port", port, "--no-access-log"])
        while not STOP.wait(0.5):
            check_children()
        return 0
    except InterruptedError:
        return 0
    except Exception as exc:
        # The launcher never receives member IDs, goals, outputs or credentials.
        print(f"relay: startup/runtime failure: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
