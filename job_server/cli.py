"""Command-line administration of the Job Server on this PC.

Lifecycle commands (start, stop, restart, status, logs) act on this PC's
daemon with the authority of the OS account that runs it, like create-admin
and reset-password. Everything else goes through the same authenticated API
as the desktop window: ``login`` registers this command line as a device of
its own, saved as ``identity/cli-device.key`` in the data folder, and later
commands sign in with that key. A password is always asked for, never taken
from the command line.
"""

import argparse
import getpass
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path


class CliError(RuntimeError):
    """A failure reported as one line, with the exit status 1."""


def _program():
    """How this command was started, so a hint names a command that exists.

    Started by ``moleditpy --run-plugin`` the server is a plugin folder, and
    the ``moleditpy-job-server`` launcher pip creates need not be installed.
    """
    if "moleditpy.plugins.plugin_runner" in sys.modules:
        return f"moleditpy -r {__package__ or 'job_server'}"
    return "moleditpy-job-server"


def _command(action, args):
    """The command line for a next step, carrying a non-default --config along."""
    config = getattr(args, "config", None)
    return f"{_program()} {action}" + (f' --config "{config}"' if config else "")


def key_path(settings):
    """This command line's device credential; the window keeps its own."""
    return settings.root / "identity" / "cli-device.key"


def config_path(args, settings):
    """The configuration file a started daemon reads."""
    return Path(args.config) if getattr(args, "config", None) else settings.root / "settings.json"


def _time(value):
    from .lifecycle import TIME_FORMAT

    return datetime.fromtimestamp(value).strftime(TIME_FORMAT) if value else ""


def _short(identifier):
    return str(identifier or "")[:8]


def _mb(value):
    if value is None:
        return ""
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value} MB"


def _table(rows, columns, out=None):
    """Print *rows* under *columns*, a list of (heading, function of a row)."""
    out = out or sys.stdout
    cells = [[str(get(row)) for _, get in columns] for row in rows]
    widths = [
        max([len(heading)] + [len(line[index]) for line in cells])
        for index, (heading, _) in enumerate(columns)
    ]
    print("  ".join(h.ljust(w) for (h, _), w in zip(columns, widths)).rstrip(), file=out)
    print("  ".join("-" * w for w in widths), file=out)
    for line in cells:
        print("  ".join(c.ljust(w) for c, w in zip(line, widths)).rstrip(), file=out)


def _json(data):
    print(json.dumps(data, indent=2, default=str))


def _confirm(prompt, args):
    """Ask before an action that cannot be undone; --yes answers for scripts."""
    if getattr(args, "yes", False):
        return True
    if not sys.stdin or not sys.stdin.isatty():
        raise CliError(f"{prompt} Add --yes to confirm when there is no terminal.")
    answer = input(f"{prompt} [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        raise CliError("Cancelled; nothing was changed.")
    return True


def _ask_password(prompt="Password: ", repeat=False):
    from .auth import MIN_PASSWORD_LENGTH

    password = getpass.getpass(prompt)
    if repeat:
        if password != getpass.getpass("Repeat password: "):
            raise CliError("Passwords differ")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise CliError(f"The password needs at least {MIN_PASSWORD_LENGTH} characters")
    return password


# --- the server on this PC -------------------------------------------------


def _require_running(args, settings):
    from .lifecycle import directory_is_idle

    if not settings.root.exists() or directory_is_idle(settings.root):
        raise CliError(f"Server is OFF. Start it with: {_command('start', args)}")


def _verify(settings):
    """Fail closed before any credential goes to the loopback port."""
    from .daemon_ownership import verify_local_daemon

    verify_local_daemon(settings.root, settings.port)


def _client(settings, timeout=10):
    from .client import JobServerClient
    from .lifecycle import local_url

    return JobServerClient(local_url(settings), key_path(settings), timeout=timeout)


def _signed_client(args, settings):
    """A client that signs in with this command line's device key."""
    _require_running(args, settings)
    if not key_path(settings).is_file():
        raise CliError(f"Not signed in. Sign in first with: {_command('login', args)}")
    _verify(settings)
    return _client(settings)


def _request(client, method, path, body=None):
    from .client import ClientError

    try:
        return client.request(method, path, body)
    except ClientError as exc:
        if exc.status == 401:
            raise CliError(
                f"{exc}. The saved sign-in was not accepted; it may have been revoked. "
                "Sign in again with logout --local-only, then login."
            ) from None
        if exc.status == 403:
            raise CliError(f"{exc}. This needs an administrator account.") from None
        raise CliError(str(exc)) from None


def _owner_pid(settings):
    from .daemon_ownership import descriptor_path

    try:
        return json.loads(descriptor_path(settings.root).read_text("utf-8")).get("pid")
    except (OSError, ValueError, AttributeError):
        return None


def cmd_start(args, settings):
    from .lifecycle import start_daemon

    config = config_path(args, settings)
    if not config.is_file():
        raise CliError(f"No configuration at {config}. Create one with: {_command('init', args)}")
    try:
        started = start_daemon(settings, config)
    except (RuntimeError, TimeoutError) as exc:
        raise CliError(f"{exc} Log: {settings.root / 'logs' / 'startup.log'}") from None
    pid = _owner_pid(settings)
    print(("Server started" if started else "Server is already running")
          + (f" (pid {pid})." if pid else "."))
    for url in [_local_url(settings), *settings.connection_urls]:
        print(f"  {url}")
    return 0


def _local_url(settings):
    from .lifecycle import local_url

    return local_url(settings)


def _stop(args, settings):
    """Stop the daemon; running jobs stay with their supervisors. True if one ran."""
    from .lifecycle import directory_is_idle, stop_and_wait, stop_processes_and_wait

    if not settings.root.exists() or directory_is_idle(settings.root):
        return False
    if key_path(settings).is_file():
        try:
            _verify(settings)
            # Signed shutdown records who stopped the server in the event log.
            stop_and_wait(_client(settings), settings.root)
            return True
        except Exception as exc:
            if not args.force:
                raise CliError(
                    f"Signed shutdown failed: {exc}. Use --force to stop the server process "
                    "on this PC instead."
                ) from None
    elif os.name == "nt" and not args.force:
        # Windows has no SIGTERM: ending the process skips the clean shutdown.
        raise CliError(
            f"Not signed in. Sign in with {_command('login', args)} for a clean shutdown, "
            "or add --force to end the server process. Running jobs are kept either way."
        )
    # POSIX: SIGTERM, which serve handles as a clean shutdown.
    stop_processes_and_wait(settings.root)
    return True


def cmd_stop(args, settings):
    if _stop(args, settings):
        print("Server stopped. Running jobs continue under their supervisors.")
    else:
        print("Server is already OFF.")
    return 0


def cmd_restart(args, settings):
    if _stop(args, settings):
        print("Server stopped. Running jobs continue under their supervisors.")
    return cmd_start(args, settings)


def cmd_status(args, settings):
    from .client import JobServerClient

    _require_running(args, settings)
    health = JobServerClient(_local_url(settings), timeout=3).request(
        "GET", "/health", authenticated=False
    )
    queue = None
    if key_path(settings).is_file():
        try:
            _verify(settings)
            queue = _client(settings).request("GET", "/system/resources")
        except Exception:
            queue = None  # status still answers when the saved sign-in is stale
    result = {
        **health,
        "pid": _owner_pid(settings),
        "urls": [_local_url(settings), *settings.connection_urls],
        "resources": queue,
    }
    if args.json:
        _json(result)
        return 0
    state = "ON" if health.get("status") == "ok" else health.get("status", "?").upper()
    print(f"Server is {state}: Job Server {health.get('server_version', '?')}"
          f" on {health.get('hostname', '?')}" + (f" (pid {result['pid']})" if result["pid"] else ""))
    if health.get("label"):
        print(f"  {health['label']}")
    if health.get("description"):
        print(f"  {health['description']}")
    for url in result["urls"]:
        print(f"  {url}")
    if queue:
        print(
            f"Jobs: {queue['running']} running, {queue['pending']} waiting"
            + (" (queue PAUSED)" if queue["paused"] else "")
        )
        print(
            f"Reserved: {queue['reserved_cpus']}/{queue['cpus']} CPUs, "
            f"{_mb(queue['reserved_memory_mb'])}/{_mb(queue['memory_mb'])}"
        )
    else:
        print(f"Sign in with {_command('login', args)} to also see the queue.")
    return 0


def cmd_logs(args, settings):
    directory = settings.root / "logs"
    files = sorted(directory.glob("*.log")) if directory.is_dir() else []
    if not args.name:
        print(f"Log folder: {directory}")
        for path in files:
            print(f"  {path.stem:<10} {path.stat().st_size:>10} bytes  {path.name}")
        if not files:
            print("  (no logs yet)")
        return 0
    path = directory / (args.name if args.name.endswith(".log") else args.name + ".log")
    if path.parent != directory or not path.is_file():
        names = ", ".join(p.stem for p in files) or "none yet"
        raise CliError(f"No log named {args.name!r} in {directory} (available: {names})")
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        # The last --lines lines, read from the end so a long log is not loaded whole.
        start, block, data = size, 65536, b""
        while start > 0 and data.count(b"\n") <= args.lines:
            start = max(0, start - block)
            stream.seek(start)
            data = stream.read(size - start)
        lines = data.splitlines()[-args.lines:] if args.lines else []
        for line in lines:
            print(line.decode("utf-8", errors="replace"))
        if not args.follow:
            return 0
        stream.seek(size)
        try:
            while True:
                chunk = stream.read()
                if chunk:
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


def cmd_drain(args, settings):
    from .lifecycle import stop_and_wait

    client = _signed_client(args, settings)
    _request(client, "POST", "/admin/queue/pause", {"paused": True})
    print("Queue paused: no waiting job will start.")
    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    last = None
    try:
        while True:
            running = _request(client, "GET", "/system/resources")["running"]
            if running != last:
                print(f"Waiting for {running} running job(s) to finish..." if running
                      else "No job is running.")
                last = running
            if not running:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise CliError(
                    f"{running} job(s) still running after {args.timeout:g} s. The queue stays "
                    f"paused; resume it with: {_command('queue resume', args)}"
                )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        raise CliError(
            f"Interrupted. The queue stays paused; resume it with: {_command('queue resume', args)}"
        ) from None
    if args.no_stop:
        print(f"The queue stays paused; resume it with: {_command('queue resume', args)}")
        return 0
    stop_and_wait(client, settings.root)
    print("Server stopped. The queue stays paused after the next start; resume it with: "
          + _command("queue resume", args))
    return 0


# --- signing in ------------------------------------------------------------


def cmd_login(args, settings):
    _require_running(args, settings)
    _verify(settings)
    path = key_path(settings)
    if path.is_file():
        me = _request(_client(settings), "GET", "/me")
        print(f"Already signed in as {me['username']} ({me['role']}).")
        return 0
    username = args.username or input("Username: ").strip()
    if not username:
        raise CliError("A username is needed")
    password = _ask_password()
    from .client import ClientError

    client = _client(settings)
    try:
        client.enroll(username, password)
    except ClientError as exc:
        path.unlink(missing_ok=True)
        raise CliError(f"Sign-in failed: {exc}") from None
    me = _request(client, "GET", "/me")
    from .credentials import load_key

    device = load_key(path)["device_id"]
    print(f"Signed in as {me['username']} ({me['role']}). "
          f"This command line is registered as device {_short(device)}.")
    return 0


def cmd_logout(args, settings):
    from .credentials import load_key

    path = key_path(settings)
    if not path.is_file():
        print("Not signed in.")
        return 0
    if not args.local_only:
        client = _signed_client(args, settings)
        device = load_key(path)["device_id"]
        _request(client, "POST", f"/me/devices/{device}/revoke")
        print(f"Device {_short(device)} revoked on the server.")
    path.unlink()
    print("Signed out; the saved device key is deleted.")
    if args.local_only:
        print("The device stays registered on the server until an administrator revokes it.")
    return 0


def cmd_whoami(args, settings):
    from .credentials import load_key

    me = _request(_signed_client(args, settings), "GET", "/me")
    if args.json:
        _json(me)
        return 0
    device = load_key(key_path(settings))["device_id"]
    print(f"{me['username']} ({me['role']}), device {_short(device)} on {_local_url(settings)}")
    return 0


# --- queue and jobs ----------------------------------------------------------


def cmd_queue(args, settings):
    client = _signed_client(args, settings)
    action = getattr(args, "queue_action", None) or "show"
    if action in {"pause", "resume"}:
        _request(client, "POST", "/admin/queue/pause", {"paused": action == "pause"})
        print("Queue paused: no waiting job will start." if action == "pause"
              else "Queue resumed.")
        return 0
    queue = _request(client, "GET", "/queue")
    if args.json:
        _json(queue)
        return 0
    print("Queue is PAUSED." if queue["paused"] else "Queue is running.")
    print(f"\nRunning ({len(queue['running'])}):")
    if queue["running"]:
        _table(queue["running"], [
            ("Job", lambda r: _short(r["job_id"]) or "-"), ("Name", lambda r: r["name"]),
            ("Owner", lambda r: r["owner"]), ("CPUs", lambda r: r["cpus"]),
            ("Memory", lambda r: _mb(r["memory_mb"])), ("Started", lambda r: _time(r["started_at"])),
        ])
    print(f"\nWaiting ({len(queue['waiting'])}):")
    if queue["waiting"]:
        _table(queue["waiting"], [
            ("#", lambda r: r["position"]), ("Job", lambda r: _short(r["job_id"]) or "-"),
            ("Name", lambda r: r["name"]), ("Owner", lambda r: r["owner"]),
            ("State", lambda r: r["display_state"] + (" FORCE" if r["force_run"] else "")),
            ("Priority", lambda r: r["priority"]), ("CPUs", lambda r: r["cpus"]),
            ("Memory", lambda r: _mb(r["memory_mb"])),
        ])
    return 0


def _job_list(client, everyone, limit=1000):
    return _request(client, "GET", ("/admin/jobs" if everyone else "/jobs") + f"?limit={limit}")


def _resolve_job(client, text):
    """A full job ID, or the job whose ID starts with *text* (at least 4 characters)."""
    try:
        return str(uuid.UUID(text))
    except ValueError:
        pass
    text = text.lower()
    if len(text) < 4:
        raise CliError("Give at least the first 4 characters of the job ID")
    try:
        jobs = _job_list(client, True)
    except CliError:
        jobs = _job_list(client, False)  # not an administrator: own jobs only
    matches = [job["id"] for job in jobs if job["id"].startswith(text)]
    if len(matches) != 1:
        raise CliError(
            f"No job ID starts with {text!r}" if not matches
            else f"{len(matches)} job IDs start with {text!r}; give more characters"
        )
    return matches[0]


def _user_names(client):
    try:
        return {user["id"]: user["username"] for user in _request(client, "GET", "/admin/users")}
    except CliError:
        return {}


def _print_job(job):
    spec = job.get("spec", {})
    resources = spec.get("resources", {})
    fields = [
        ("ID", job["id"]), ("Name", spec.get("name", "")),
        ("State", job.get("display_state", job["state"])), ("Reason", job.get("reason", "")),
        ("Priority", job.get("priority")), ("Held", "yes" if job.get("held") else "no"),
        ("Force run", "yes" if job.get("force_run") else "no"),
        ("CPUs", resources.get("cpus_per_task")), ("Memory", _mb(resources.get("memory_mb"))),
        ("Created", _time(job.get("created_at"))), ("Started", _time(job.get("started_at"))),
        ("Ended", _time(job.get("ended_at"))), ("Exit code", job.get("exit_code")),
        ("Waiting ahead", job.get("queue_ahead")), ("Command", spec.get("command", "")),
    ]
    for label, value in fields:
        if value not in (None, ""):
            print(f"{label + ':':<15}{value}")


def cmd_jobs(args, settings):
    client = _signed_client(args, settings)
    action = getattr(args, "jobs_action", None) or "list"
    if action == "list":
        jobs = _job_list(client, args.all, args.limit)
        if args.state:
            wanted = {s.upper() for s in args.state}
            jobs = [j for j in jobs if j["state"] in wanted or j.get("display_state") in wanted]
        if args.json:
            _json(jobs)
            return 0
        names = _user_names(client) if args.all else {}
        columns = [
            ("Job", lambda j: _short(j["id"])), ("Name", lambda j: j["spec"].get("name", "")),
            ("State", lambda j: j.get("display_state", j["state"])),
            ("CPUs", lambda j: j["spec"]["resources"]["cpus_per_task"]),
            ("Memory", lambda j: _mb(j["spec"]["resources"]["memory_mb"])),
            ("Created", lambda j: _time(j["created_at"])), ("Ended", lambda j: _time(j["ended_at"])),
        ]
        if args.all:
            columns.insert(2, ("Owner", lambda j: names.get(j["owner_id"], _short(j["owner_id"]))))
        if jobs:
            _table(jobs, columns)
        else:
            print("No jobs.")
        return 0
    if action == "move-finished":
        result = _request(client, "POST", "/admin/jobs/move-finished")
        print(f"{result['queued']} finished job(s) queued for {result['destination']}.")
        return 0

    job_id = _resolve_job(client, args.job)
    route = "/jobs/" + job_id
    if action == "show":
        job = _request(client, "GET", route)
        if args.json:
            _json(job)
        else:
            _print_job(job)
        return 0
    if action == "log":
        return _job_log(client, route, args)
    if action == "cancel":
        _confirm(f"Cancel job {_short(job_id)}?", args)
        _request(client, "POST", route + "/cancel")
        print(f"Cancellation of job {_short(job_id)} requested.")
        return 0
    if action in {"hold", "release"}:
        _request(client, "POST", route + "/hold", {"held": action == "hold"})
        print(f"Job {_short(job_id)} " + ("held: it will not start." if action == "hold"
                                         else "released."))
        return 0
    admin_route = "/admin/jobs/" + job_id
    if action == "priority":
        _request(client, "POST", admin_route + "/priority", {"priority": args.priority})
        print(f"Job {_short(job_id)} priority set to {args.priority}.")
    elif action == "resources":
        current = _request(client, "GET", route)["spec"]["resources"]
        body = {
            "cpus_per_task": current["cpus_per_task"] if args.cpus is None else args.cpus,
            "memory_mb": current["memory_mb"] if args.memory_mb is None else args.memory_mb,
        }
        _request(client, "POST", admin_route + "/resources", body)
        print(f"Job {_short(job_id)} now reserves {body['cpus_per_task']} CPUs and "
              f"{_mb(body['memory_mb'])}.")
    elif action == "owner":
        user = _find_user(client, args.username)
        _request(client, "POST", admin_route + "/owner", {"user_id": user["id"]})
        print(f"Job {_short(job_id)} now belongs to {user['username']}.")
    elif action == "force-run":
        if not args.undo:
            _confirm(f"Run job {_short(job_id)} even if it exceeds the free budget?", args)
        _request(client, "POST", admin_route + "/force-run", {"force_run": not args.undo})
        print(f"Force-run request for job {_short(job_id)} "
              + ("withdrawn." if args.undo else "recorded."))
    elif action == "mark-lost":
        _confirm(f"Mark job {_short(job_id)} as lost? Its reservation is released.", args)
        _request(client, "POST", admin_route + "/mark-lost")
        print(f"Job {_short(job_id)} marked lost.")
    return 0


def _job_log(client, route, args):
    offset = 0
    if args.tail is not None:
        size = _request(client, "GET", route + "/log?limit=1").get("size", 0)
        offset = max(0, size - args.tail)
    ended = False
    try:
        while True:
            chunk = _request(client, "GET", route + f"/log?offset={offset}&limit=1048576")
            if chunk["text"]:
                sys.stdout.write(chunk["text"])
                sys.stdout.flush()
            offset = chunk["next_offset"]
            if chunk["text"] and offset < chunk.get("size", offset):
                continue  # more is already written
            if not args.follow or ended:
                return 0
            # Read once more after the job ends, for what it wrote last.
            ended = _request(client, "GET", route)["state"] in {"DONE", "FAILED", "CANCELLED", "LOST"}
            if not ended:
                time.sleep(2)
    except KeyboardInterrupt:
        return 0


# --- users, devices and events -----------------------------------------------


def _find_user(client, username):
    for user in _request(client, "GET", "/admin/users"):
        if user["username"] == username:
            return user
    raise CliError(f"No user named {username!r}")


def cmd_users(args, settings):
    client = _signed_client(args, settings)
    action = getattr(args, "users_action", None) or "list"
    if action == "list":
        users = _request(client, "GET", "/admin/users")
        if args.json:
            _json(users)
            return 0
        _table(users, [
            ("Username", lambda u: u["username"]), ("Role", lambda u: u["role"]),
            ("Enabled", lambda u: "yes" if u["enabled"] else "no"),
            ("Created", lambda u: _time(u["created_at"])),
        ])
        return 0
    if action == "add":
        password = _ask_password("Initial password: ", repeat=True)
        role = "admin" if args.admin else "user"
        _request(client, "POST", "/admin/users",
                 {"username": args.username, "password": password, "role": role})
        print(f"{'Administrator' if args.admin else 'User'} {args.username} created.")
        return 0
    user = _find_user(client, args.username)
    route = "/admin/users/" + user["id"]
    if action in {"disable", "enable"}:
        if action == "disable":
            _confirm(f"Disable {user['username']}? Their devices stop working until enabled.", args)
        _request(client, "POST", route + "/" + action)
        print(f"{user['username']} {action}d.")
    elif action == "delete":
        _confirm(f"Delete {user['username']}? This cannot be undone.", args)
        _request(client, "DELETE", route)
        print(f"{user['username']} deleted.")
    elif action == "role":
        _request(client, "POST", route + "/role", {"role": args.role})
        print(f"{user['username']} is now {'an administrator' if args.role == 'admin' else 'a user'}.")
    elif action == "password":
        password = _ask_password(f"New password for {user['username']}: ", repeat=True)
        _request(client, "POST", route + "/password",
                 {"new_password": password, "revoke_devices": args.revoke_devices})
        print(f"Password of {user['username']} changed"
              + (" and their devices revoked." if args.revoke_devices else "."))
    return 0


def cmd_devices(args, settings):
    client = _signed_client(args, settings)
    devices = _request(client, "GET", "/admin/devices")
    if (getattr(args, "devices_action", None) or "list") == "list":
        if args.json:
            _json(devices)
            return 0
        names = _user_names(client)
        from .credentials import load_key

        mine = load_key(key_path(settings))["device_id"]
        _table(devices, [
            ("Device", lambda d: _short(d["id"]) + (" (this)" if d["id"] == mine else "")),
            ("User", lambda d: names.get(d["user_id"], _short(d["user_id"]))),
            ("Registered", lambda d: _time(d["created_at"])),
            ("Last access", lambda d: _time(d["last_access"])),
            ("Revoked", lambda d: _time(d["revoked_at"]) or "-"),
        ])
        return 0
    matches = [d for d in devices if d["id"].startswith(args.device.lower())]
    if len(matches) != 1:
        raise CliError(f"No device ID starts with {args.device!r}" if not matches
                       else f"{len(matches)} device IDs start with {args.device!r}")
    device = matches[0]
    _confirm(f"Revoke device {_short(device['id'])}? It must sign in again to be used.", args)
    _request(client, "POST", f"/admin/devices/{device['id']}/revoke")
    print(f"Device {_short(device['id'])} revoked.")
    return 0


def cmd_events(args, settings):
    client = _signed_client(args, settings)
    events = list(reversed(_request(client, "GET", f"/admin/events?newest={args.number}")))
    if args.json:
        _json(events)
        return 0
    names = _user_names(client)
    _table(events, [
        ("Seq", lambda e: e["seq"]), ("Time", lambda e: _time(e["timestamp"])),
        ("Event", lambda e: e["event"]),
        ("Actor", lambda e: names.get(e["actor"], _short(e["actor"])) if e["actor"] else ""),
        ("Job", lambda e: _short(e["job_id"])),
        ("Details", lambda e: "" if e["details"] in ("{}", "") else e["details"]),
    ])
    return 0


# --- the parser ----------------------------------------------------------------


def _parser(sub, name, handler, help_text, parents=()):
    child = sub.add_parser(name, help=help_text, description=help_text, parents=list(parents))
    child.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    child.set_defaults(handler=handler)
    return child


def add_commands(sub):
    """Add the administration commands to the top-level subcommand parser."""
    yes = argparse.ArgumentParser(add_help=False)
    yes.add_argument("-y", "--yes", action="store_true", help="answer yes to confirmations")
    as_json = argparse.ArgumentParser(add_help=False)
    as_json.add_argument("--json", action="store_true", help="print JSON")

    _parser(sub, "start", cmd_start, "start the server in the background")
    for name, handler, text in (
        ("stop", cmd_stop, "stop the server; running jobs continue"),
        ("restart", cmd_restart, "stop and start the server; running jobs continue"),
    ):
        child = _parser(sub, name, handler, text)
        child.add_argument(
            "--force", action="store_true",
            help="stop the server process when a signed shutdown is not possible",
        )
    _parser(sub, "status", cmd_status, "show whether the server runs, and its queue",
            [as_json])
    logs = _parser(sub, "logs", cmd_logs, "list the server logs, or show one")
    logs.add_argument("name", nargs="?", help="daemon, startup or gui")
    logs.add_argument("-n", "--lines", type=int, default=50, help="lines to show (default 50)")
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    drain = _parser(sub, "drain", cmd_drain,
                    "pause the queue, wait for running jobs to finish, then stop the server")
    drain.add_argument("--timeout", type=float, help="give up after this many seconds")
    drain.add_argument("--interval", type=float, default=10, help=argparse.SUPPRESS)
    drain.add_argument("--no-stop", action="store_true",
                       help="leave the server running once no job runs")

    login = _parser(sub, "login", cmd_login, "register this command line and sign in")
    login.add_argument("username", nargs="?")
    logout = _parser(sub, "logout", cmd_logout, "revoke this command line's device and sign out")
    logout.add_argument("--local-only", action="store_true",
                        help="only delete the saved key, without asking the server")
    _parser(sub, "whoami", cmd_whoami, "show the signed-in account", [as_json])

    queue = _parser(sub, "queue", cmd_queue, "show, pause or resume the queue", [as_json])
    actions = queue.add_subparsers(dest="queue_action")
    for name, text in (("show", "show running and waiting jobs"),
                       ("pause", "keep waiting jobs from starting"),
                       ("resume", "let waiting jobs start again")):
        _parser(actions, name, cmd_queue, text, [as_json])

    jobs = _parser(sub, "jobs", cmd_jobs, "list and manage jobs")
    actions = jobs.add_subparsers(dest="jobs_action")
    listing = _parser(actions, "list", cmd_jobs, "list jobs (your own; --all for everyone's)",
                      [as_json])
    for target in (jobs, listing):
        target.add_argument("--all", action="store_true", help="every user's jobs (administrator)")
        target.add_argument("--state", action="append", help="only this state (repeatable)")
        target.add_argument("--limit", type=int, default=100, help="at most this many (default 100)")
        if target is jobs:
            target.add_argument("--json", action="store_true", help="print JSON")
    one = argparse.ArgumentParser(add_help=False)
    one.add_argument("job", help="job ID, or its first characters")
    _parser(actions, "show", cmd_jobs, "show one job", [one, as_json])
    log = _parser(actions, "log", cmd_jobs, "print a job's log", [one])
    log.add_argument("-f", "--follow", action="store_true", help="keep printing until it ends")
    log.add_argument("--tail", type=int, metavar="BYTES", help="start this many bytes from the end")
    _parser(actions, "cancel", cmd_jobs, "cancel a job", [one, yes])
    _parser(actions, "hold", cmd_jobs, "keep a waiting job from starting", [one])
    _parser(actions, "release", cmd_jobs, "let a held job start", [one])
    priority = _parser(actions, "priority", cmd_jobs, "set a waiting job's priority", [one])
    priority.add_argument("priority", type=int, help="-1000 to 1000; higher starts first")
    resources = _parser(actions, "resources", cmd_jobs,
                        "change what a waiting job reserves", [one])
    resources.add_argument("--cpus", type=int)
    resources.add_argument("--memory-mb", type=int)
    owner = _parser(actions, "owner", cmd_jobs, "give a job to another user", [one])
    owner.add_argument("username")
    force = _parser(actions, "force-run", cmd_jobs,
                    "start a waiting job even beyond the free budget", [one, yes])
    force.add_argument("--undo", action="store_true", help="withdraw the request")
    _parser(actions, "mark-lost", cmd_jobs,
            "release a stuck job the server can no longer follow", [one, yes])
    _parser(actions, "move-finished", cmd_jobs,
            "queue finished jobs for the finished-jobs folder")

    users = _parser(sub, "users", cmd_users, "list and manage user accounts (administrator)",
                    [as_json])
    actions = users.add_subparsers(dest="users_action")
    _parser(actions, "list", cmd_users, "list users", [as_json])
    name = argparse.ArgumentParser(add_help=False)
    name.add_argument("username")
    add = _parser(actions, "add", cmd_users, "add a user; the password is asked for", [name])
    add.add_argument("--admin", action="store_true", help="make it an administrator")
    _parser(actions, "disable", cmd_users, "disable a user", [name, yes])
    _parser(actions, "enable", cmd_users, "enable a user", [name])
    _parser(actions, "delete", cmd_users, "delete a user", [name, yes])
    role = _parser(actions, "role", cmd_users, "make a user an administrator or not", [name])
    role.add_argument("role", choices=["user", "admin"])
    password = _parser(actions, "password", cmd_users, "set a user's password", [name])
    password.add_argument("--revoke-devices", action="store_true",
                          help="also revoke the user's registered devices")

    devices = _parser(sub, "devices", cmd_devices, "list or revoke registered devices",
                      [as_json])
    actions = devices.add_subparsers(dest="devices_action")
    _parser(actions, "list", cmd_devices, "list devices", [as_json])
    revoke = _parser(actions, "revoke", cmd_devices, "revoke a device", [yes])
    revoke.add_argument("device", help="device ID, or its first characters")

    events = _parser(sub, "events", cmd_events, "show the newest audit events", [as_json])
    events.add_argument("-n", "--number", type=int, default=50, help="how many (default 50)")
