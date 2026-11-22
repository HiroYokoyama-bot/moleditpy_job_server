"""Bounded lifecycle waits for the local administration client."""

import os
import subprocess
import time
from datetime import datetime
from http.client import HTTPException
from pathlib import Path

import psutil

from .daemon_ownership import daemon_processes, verify_local_daemon
from .instance import DaemonAlreadyRunning, InstanceLock


#: Local time without a UTC offset, matching the dashboard.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def startup_banner(now=None):
    """The line written to startup.log before each daemon launch."""
    from . import PLUGIN_VERSION

    stamp = (now or datetime.now()).strftime(TIME_FORMAT)
    return "\n===== " + stamp + " Starting Job Server " + PLUGIN_VERSION + " =====\n"


def directory_is_idle(root):
    probe = InstanceLock(root)
    try:
        probe.acquire()
    except DaemonAlreadyRunning:
        return False
    probe.release()
    return True


def wait_until_ready(client, root, process, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        if process is not None and process.poll() is not None:
            raise RuntimeError("Server exited during startup. Open File > Open Logs > startup.log.")
        try:
            health = client.request("GET", "/health", authenticated=False)
        except (OSError, HTTPException, ValueError):
            pass
        else:
            from urllib.parse import urlsplit

            if health.get("status") == "ok":
                verify_local_daemon(root, urlsplit(client.url).port)
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("Server did not become ready. Open File > Open Logs > startup.log.")
        time.sleep(0.15)


def local_url(settings):
    """The loopback address local administration talks to."""
    return f"http://127.0.0.1:{settings.port}"


def start_daemon(settings, config, before_launch=None, probe=None):
    """Start this configuration's daemon detached, or reuse the one running.

    Returns True when a new daemon was started, False when this directory's
    own daemon was already running. A listener on the port that is not this
    Job Server raises, and nothing is started. The daemon outlives the caller:
    it gets a session of its own, and its output goes to logs/startup.log.
    *before_launch* runs only when a new daemon is about to be started; *probe*
    is the client that asks the port for /health first (default: a new one).
    """
    from .client import JobServerClient
    from .runtime import python_executable

    url = local_url(settings)
    try:
        health = (probe or JobServerClient(url, timeout=3)).request(
            "GET", "/health", authenticated=False
        )
    except Exception:
        health = None
    if health is not None:
        verify_local_daemon(settings.root, settings.port)
        if health.get("status", "ok") != "ok":
            wait_until_ready(JobServerClient(url, timeout=1), settings.root, None)
        return False
    if before_launch is not None:
        before_launch()
    log_dir = settings.root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[1]) + os.pathsep + environment.get("PYTHONPATH", "")
    )
    with (log_dir / "startup.log").open("ab") as log:
        log.write(startup_banner().encode("utf-8"))
        log.flush()
        process = subprocess.Popen(
            [python_executable(settings), "-m", "job_server", "serve", "--config", str(config)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=os.name != "nt",
            creationflags=0x08000000 | 0x00000008 if os.name == "nt" else 0,
        )
    wait_until_ready(JobServerClient(url, timeout=1), settings.root, process)
    return True


def _wait_until_idle(root, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if directory_is_idle(root):
            return True
        time.sleep(0.15)
    return directory_is_idle(root)


def stop_processes_and_wait(root, timeout=20):
    """Stop this directory's daemon by process when it cannot be verified.

    The fallback for a daemon verify_local_daemon rejects — an older
    daemon without an owner descriptor, or one whose port changed while
    it ran. The authenticated shutdown in stop_and_wait is unreachable
    then, and requiring this window's device registration first would
    make such a daemon unstoppable from the window (registration itself
    verifies ownership). Terminating the same-account process needs no
    credentials; job supervisors are separate processes and are
    preserved. Call only after the user confirms in the window.
    """
    processes = daemon_processes(root)
    if not processes:
        raise RuntimeError(
            "No Job Server process for this data directory was found. "
            "Stop the old daemon from the tool that started it."
        )
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    if _wait_until_idle(root, timeout):
        return
    for process in processes:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    if not _wait_until_idle(root, 5):
        raise TimeoutError(
            "Server process stop was not confirmed. Open File > Open Logs before retrying."
        )


#: Seconds to wait for a stopping daemon to release its data folder. Longer than its
#: own worst case: 10 s for open requests, then up to 30 s for maintenance and
#: 5 s each for the dispatcher and the load history.
STOP_TIMEOUT = 60


def stop_and_wait(client, root, timeout=STOP_TIMEOUT):
    from urllib.parse import urlsplit

    if directory_is_idle(root):
        return
    verify_local_daemon(root, urlsplit(client.url).port)
    network_error = None
    try:
        client.request("POST", "/admin/shutdown")
    except (OSError, HTTPException) as exc:
        # The process may close the connection after accepting shutdown.
        network_error = exc
    deadline = time.monotonic() + timeout
    while True:
        if directory_is_idle(root):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Server shutdown was not confirmed. Open File > Open Logs before retrying."
            ) from network_error
        time.sleep(0.15)
