"""The administration command line: lifecycle, sign-in and API commands."""

import json
import socket
import sys
import types
import uuid

import pytest

from job_server import cli
from job_server.__main__ import main
from job_server.settings import Settings


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["moleditpy-job-server", *argv])
    return main()


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- against a real daemon ---------------------------------------------------


def test_real_daemon_through_the_command_line(monkeypatch, tmp_path, capsys):
    from job_server.auth import Auth
    from job_server.db import Database
    from job_server.lifecycle import directory_is_idle, stop_processes_and_wait
    from job_server.runtime import ensure_secure_dependencies

    try:
        ensure_secure_dependencies()
    except RuntimeError:
        pytest.skip("Real daemon requires the security dependency floors")
    settings = Settings(data_dir=str(tmp_path), port=free_port(), allow_privileged=True)
    config = tmp_path / "settings.json"
    settings.save(config)
    db = Database(settings.root)
    try:
        Auth(db, settings).create_initial_admin("fixture-admin", "synthetic-password")
    finally:
        db.close()
    passwords = iter(["synthetic-password", "second-password", "second-password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(passwords))
    c = ("--config", str(config))
    try:
        assert run(monkeypatch, "status", *c) == 1
        assert "Server is OFF" in capsys.readouterr().err

        assert run(monkeypatch, "start", *c) == 0
        assert "Server started" in capsys.readouterr().out
        assert run(monkeypatch, "start", *c) == 0
        assert "already running" in capsys.readouterr().out

        assert run(monkeypatch, "queue", *c) == 1
        assert "Not signed in" in capsys.readouterr().err
        assert run(monkeypatch, "login", "fixture-admin", *c) == 0
        assert "Signed in as fixture-admin (admin)" in capsys.readouterr().out
        assert cli.key_path(settings).is_file()
        assert run(monkeypatch, "login", *c) == 0
        assert "Already signed in" in capsys.readouterr().out

        assert run(monkeypatch, "whoami", "--json", *c) == 0
        assert json.loads(capsys.readouterr().out)["role"] == "admin"
        assert run(monkeypatch, "status", "--json", *c) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["status"] == "ok" and status["resources"]["running"] == 0

        assert run(monkeypatch, "queue", "pause", *c) == 0
        capsys.readouterr()
        assert run(monkeypatch, "status", *c) == 0
        assert "queue PAUSED" in capsys.readouterr().out
        assert run(monkeypatch, "queue", "resume", *c) == 0
        capsys.readouterr()
        assert run(monkeypatch, "queue", "--json", *c) == 0
        assert json.loads(capsys.readouterr().out)["paused"] is False

        assert run(monkeypatch, "users", "add", "bob", *c) == 0
        assert run(monkeypatch, "users", "role", "bob", "admin", *c) == 0
        assert run(monkeypatch, "users", "disable", "bob", "--yes", *c) == 0
        capsys.readouterr()
        assert run(monkeypatch, "users", "--json", *c) == 0
        users = {u["username"]: u for u in json.loads(capsys.readouterr().out)}
        assert users["bob"]["role"] == "admin" and not users["bob"]["enabled"]
        assert run(monkeypatch, "users", "delete", "bob", "--yes", *c) == 0
        capsys.readouterr()
        assert run(monkeypatch, "jobs", *c) == 0
        assert "No jobs." in capsys.readouterr().out
        assert run(monkeypatch, "devices", *c) == 0
        assert "(this)" in capsys.readouterr().out
        assert run(monkeypatch, "events", "-n", "100", *c) == 0
        assert "queue_pause" in capsys.readouterr().out

        assert run(monkeypatch, "drain", "--interval", "0.1", *c) == 0
        out = capsys.readouterr().out
        assert "No job is running" in out and "Server stopped" in out
        assert directory_is_idle(settings.root)

        assert run(monkeypatch, "restart", *c) == 0
        assert "Server started" in capsys.readouterr().out
        assert run(monkeypatch, "queue", "resume", *c) == 0
        capsys.readouterr()
        assert run(monkeypatch, "logout", *c) == 0
        assert "revoked on the server" in capsys.readouterr().out
        assert not cli.key_path(settings).exists()
        assert run(monkeypatch, "logs", *c) == 0
        assert "startup" in capsys.readouterr().out
        assert run(monkeypatch, "logs", "startup", "-n", "5", *c) == 0
        assert "Starting Job Server" in capsys.readouterr().out
        if sys.platform != "win32":
            # Signed out on POSIX: SIGTERM, which serve handles as a clean shutdown.
            assert run(monkeypatch, "stop", *c) == 0
            assert "Server stopped" in capsys.readouterr().out
        else:
            assert run(monkeypatch, "stop", *c) == 1
            assert "--force" in capsys.readouterr().err
            assert run(monkeypatch, "stop", "--force", *c) == 0
        assert directory_is_idle(settings.root)
        assert run(monkeypatch, "stop", *c) == 0
        assert "already OFF" in capsys.readouterr().out
    finally:
        if not directory_is_idle(settings.root):
            stop_processes_and_wait(settings.root)


# --- each command against a fake API -------------------------------------------


class FakeClient:
    def __init__(self, answers):
        self.answers, self.sent = answers, []

    def request(self, method, path, body=None):
        self.sent.append((method, path, body))
        # The longest matching prefix wins, so /jobs/<id>/log beats /jobs/.
        for (m, prefix), answer in sorted(self.answers.items(), key=lambda item: -len(item[0][1])):
            if m == method and path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer(path, body) if callable(answer) else answer
        return {}


JOB = str(uuid.uuid4())
OTHER = str(uuid.uuid4())


def job_row(identifier, **extra):
    return {
        "id": identifier, "owner_id": "u1", "state": "PENDING", "display_state": "QUEUED",
        "created_at": 1.0, "ended_at": None, "priority": 0,
        "spec": {"name": "opt", "command": "run", "resources": {"cpus_per_task": 2, "memory_mb": 512}},
        **extra,
    }


@pytest.fixture
def fake(monkeypatch, tmp_path):
    settings = Settings(data_dir=str(tmp_path))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: settings))
    client = FakeClient({
        ("GET", "/admin/jobs"): [job_row(JOB), job_row(OTHER)],
        ("GET", "/jobs/"): lambda path, body: job_row(JOB),
        ("GET", "/admin/users"): [
            {"id": "u1", "username": "alice", "role": "user", "enabled": 1, "created_at": 1.0},
            {"id": "u2", "username": "bob", "role": "admin", "enabled": 1, "created_at": 1.0},
        ],
    })
    monkeypatch.setattr(cli, "_signed_client", lambda args, settings: client)
    return client


@pytest.mark.parametrize("argv,expected", [
    (["jobs", "cancel", JOB[:6], "-y"], ("POST", f"/jobs/{JOB}/cancel", None)),
    (["jobs", "hold", JOB], ("POST", f"/jobs/{JOB}/hold", {"held": True})),
    (["jobs", "release", JOB[:4]], ("POST", f"/jobs/{JOB}/hold", {"held": False})),
    (["jobs", "priority", JOB[:5], "7"], ("POST", f"/admin/jobs/{JOB}/priority", {"priority": 7})),
    (["jobs", "resources", JOB[:5], "--cpus", "4"],
     ("POST", f"/admin/jobs/{JOB}/resources", {"cpus_per_task": 4, "memory_mb": 512})),
    (["jobs", "owner", JOB[:5], "bob"], ("POST", f"/admin/jobs/{JOB}/owner", {"user_id": "u2"})),
    (["jobs", "force-run", JOB[:5], "--yes"],
     ("POST", f"/admin/jobs/{JOB}/force-run", {"force_run": True})),
    (["jobs", "force-run", JOB[:5], "--undo"],
     ("POST", f"/admin/jobs/{JOB}/force-run", {"force_run": False})),
    (["jobs", "mark-lost", JOB[:5], "-y"], ("POST", f"/admin/jobs/{JOB}/mark-lost", None)),
    (["jobs", "move-finished"], ("POST", "/admin/jobs/move-finished", None)),
    (["queue", "pause"], ("POST", "/admin/queue/pause", {"paused": True})),
    (["queue", "resume"], ("POST", "/admin/queue/pause", {"paused": False})),
    (["users", "enable", "alice"], ("POST", "/admin/users/u1/enable", None)),
    (["users", "disable", "alice", "-y"], ("POST", "/admin/users/u1/disable", None)),
    (["users", "delete", "alice", "-y"], ("DELETE", "/admin/users/u1", None)),
    (["users", "role", "alice", "admin"], ("POST", "/admin/users/u1/role", {"role": "admin"})),
])
def test_each_command_sends_its_request(fake, monkeypatch, argv, expected):
    if fake.answers.get(("POST", "/admin/jobs/move-finished")) is None:
        fake.answers[("POST", "/admin/jobs/move-finished")] = {"queued": 2, "destination": "/done"}
    assert run(monkeypatch, *argv) == 0
    assert fake.sent[-1] == expected


def test_password_commands_ask_twice_and_never_take_argv(fake, monkeypatch):
    answers = iter(["fresh-password", "fresh-password", "new-pass", "new-pass"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert run(monkeypatch, "users", "add", "carol", "--admin") == 0
    assert fake.sent[-1] == (
        "POST", "/admin/users", {"username": "carol", "password": "fresh-password", "role": "admin"}
    )
    assert run(monkeypatch, "users", "password", "alice", "--revoke-devices") == 0
    assert fake.sent[-1] == (
        "POST", "/admin/users/u1/password", {"new_password": "new-pass", "revoke_devices": True}
    )


def test_mismatched_passwords_send_nothing(fake, monkeypatch, capsys):
    answers = iter(["one-password", "another-one"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert run(monkeypatch, "users", "add", "carol") == 1
    assert "Passwords differ" in capsys.readouterr().err
    assert not any(method == "POST" for method, _, _ in fake.sent)


def test_job_prefix_must_be_unique_and_long_enough(fake, monkeypatch, capsys):
    fake.answers[("GET", "/admin/jobs")] = [job_row("abcd1111" + JOB[8:]), job_row("abcd2222" + JOB[8:])]
    assert run(monkeypatch, "jobs", "hold", "abc") == 1
    assert "at least the first 4" in capsys.readouterr().err
    assert run(monkeypatch, "jobs", "hold", "abcd") == 1
    assert "2 job IDs start with" in capsys.readouterr().err
    assert run(monkeypatch, "jobs", "hold", "ffff") == 1
    assert "No job ID starts with" in capsys.readouterr().err
    assert not any(method == "POST" for method, _, _ in fake.sent)


def test_a_user_who_is_not_an_administrator_resolves_among_own_jobs(fake, monkeypatch):
    fake.answers = {
        ("GET", "/admin/jobs"): cli.CliError("Administrator required. This needs an administrator account."),
        ("GET", "/jobs?"): [job_row(JOB)],
    }
    assert run(monkeypatch, "jobs", "hold", JOB[:6]) == 0
    assert fake.sent[-1] == ("POST", f"/jobs/{JOB}/hold", {"held": True})


def test_confirmation_without_a_terminal_needs_yes(fake, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert run(monkeypatch, "jobs", "cancel", JOB) == 1
    assert "--yes" in capsys.readouterr().err
    assert not any(path.endswith("/cancel") for _, path, _ in fake.sent)


def test_declining_a_confirmation_changes_nothing(fake, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert run(monkeypatch, "users", "delete", "alice") == 1
    assert "Cancelled" in capsys.readouterr().err
    assert not any(method == "DELETE" for method, _, _ in fake.sent)


def test_jobs_list_and_show(fake, monkeypatch, capsys):
    fake.answers[("GET", "/jobs?")] = [job_row(JOB, state="DONE", display_state="DONE")]
    assert run(monkeypatch, "jobs") == 0
    out = capsys.readouterr().out
    assert JOB[:8] in out and "DONE" in out and "Owner" not in out
    assert run(monkeypatch, "jobs", "--all", "--state", "queued") == 0
    out = capsys.readouterr().out
    assert "alice" in out and OTHER[:8] in out
    assert run(monkeypatch, "jobs", "show", JOB[:6]) == 0
    out = capsys.readouterr().out
    assert JOB in out and "Command:" in out


def test_job_log_follows_until_the_job_ends(fake, monkeypatch, capsys):
    pieces = iter([
        {"text": "one\n", "next_offset": 4, "size": 4},
        {"text": "", "next_offset": 4, "size": 4},
        {"text": "two\n", "next_offset": 8, "size": 8},
        {"text": "", "next_offset": 8, "size": 8},
    ])
    states = iter(["RUNNING", "DONE"])
    fake.answers[("GET", f"/jobs/{JOB}/log")] = lambda path, body: next(pieces)
    fake.answers[("GET", f"/jobs/{JOB}")] = lambda path, body: job_row(JOB, state=next(states))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    assert run(monkeypatch, "jobs", "log", JOB, "-f") == 0
    assert capsys.readouterr().out == "one\ntwo\n"


def test_an_unaccepted_sign_in_explains_how_to_sign_in_again(fake, monkeypatch, capsys):
    from job_server.client import ClientError

    def refused(method, path, body=None):
        raise ClientError(401, {"message": "Authentication required"})

    monkeypatch.setattr(fake, "request", refused)
    assert run(monkeypatch, "queue") == 1
    assert "logout --local-only" in capsys.readouterr().err


def test_drain_gives_up_after_its_timeout_and_keeps_the_queue_paused(fake, monkeypatch, capsys):
    fake.answers[("GET", "/system/resources")] = {"running": 2}
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    stopped = []
    monkeypatch.setattr("job_server.lifecycle.stop_and_wait", lambda *a: stopped.append(a))
    assert run(monkeypatch, "drain", "--timeout", "0") == 1
    err = capsys.readouterr().err
    assert "2 job(s) still running" in err and "queue resume" in err
    assert fake.sent[0] == ("POST", "/admin/queue/pause", {"paused": True})
    assert not stopped


# --- local checks that need no server --------------------------------------------


def test_commands_needing_the_server_report_it_is_off(monkeypatch, tmp_path, capsys):
    settings = Settings(data_dir=str(tmp_path))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: settings))
    for argv in (["status"], ["login", "alice"], ["queue"], ["drain"]):
        assert run(monkeypatch, *argv) == 1
        assert "Server is OFF" in capsys.readouterr().err


def test_start_without_a_configuration_says_how_to_create_one(monkeypatch, tmp_path, capsys):
    settings = Settings(data_dir=str(tmp_path))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: settings))
    assert run(monkeypatch, "start") == 1
    assert "init" in capsys.readouterr().err


def test_windows_stop_without_sign_in_needs_force(monkeypatch, tmp_path, capsys):
    settings = Settings(data_dir=str(tmp_path))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: settings))
    monkeypatch.setattr("job_server.lifecycle.directory_is_idle", lambda root: False)
    stopped = []
    monkeypatch.setattr("job_server.lifecycle.stop_processes_and_wait", stopped.append)
    monkeypatch.setattr(cli, "os", types.SimpleNamespace(name="nt", SEEK_END=2))
    assert run(monkeypatch, "stop") == 1
    assert "--force" in capsys.readouterr().err and not stopped
    assert run(monkeypatch, "stop", "--force") == 0
    assert stopped == [settings.root]


def test_logs_lists_shows_the_tail_and_refuses_other_paths(monkeypatch, tmp_path, capsys):
    settings = Settings(data_dir=str(tmp_path))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: settings))
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "daemon.log").write_text("".join(f"line {n}\n" for n in range(200)))
    (tmp_path / "secret.log").write_text("not a log\n")
    assert run(monkeypatch, "logs") == 0
    assert "daemon" in capsys.readouterr().out
    assert run(monkeypatch, "logs", "daemon", "-n", "3") == 0
    assert capsys.readouterr().out.splitlines() == ["line 197", "line 198", "line 199"]
    assert run(monkeypatch, "logs", "../secret") == 1
    assert "No log named" in capsys.readouterr().err


def test_logout_local_only_deletes_the_key_without_the_server(monkeypatch, tmp_path, capsys):
    settings = Settings(data_dir=str(tmp_path))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: settings))
    path = cli.key_path(settings)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    assert run(monkeypatch, "logout", "--local-only") == 0
    out = capsys.readouterr().out
    assert not path.exists() and "stays registered" in out
    assert run(monkeypatch, "logout") == 0
    assert "Not signed in" in capsys.readouterr().out


def test_hints_name_the_moleditpy_form_when_run_as_a_plugin(monkeypatch):
    monkeypatch.setitem(sys.modules, "moleditpy.plugins.plugin_runner", types.ModuleType("runner"))
    assert cli._command("login", types.SimpleNamespace(config=None)) == "moleditpy -r job_server login"


# --- the saved session -------------------------------------------------------------


def _signed_in(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from job_server.credentials import save_key

    settings = Settings(data_dir=str(tmp_path))
    save_key(cli.key_path(settings), "device-1", "server-1", Ed25519PrivateKey.generate())
    return settings


def _saved(settings, token="saved-token", expires=None):
    import time

    client = types.SimpleNamespace(
        token=token, expires=expires or time.time() + 600, authenticated_at=time.time(),
        server_id="server-1",
    )
    cli._CLIENTS.append((settings, client))
    cli.remember_sessions()


def test_a_saved_token_is_reused_and_protected(tmp_path):
    import os

    settings = _signed_in(tmp_path)
    _saved(settings)
    raw = cli.session_path(settings).read_bytes()
    if os.name == "nt":
        assert raw.startswith(b"DPAPI\n") and b"saved-token" not in raw
    else:
        assert cli.session_path(settings).stat().st_mode & 0o077 == 0
    client = cli._client(settings)
    assert client.token == "saved-token" and client.cached_session
    cli._CLIENTS.clear()


def test_an_expired_or_foreign_session_is_not_used(tmp_path):
    import time

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from job_server.credentials import save_key

    settings = _signed_in(tmp_path)
    _saved(settings, expires=time.time() - 1)
    assert cli._client(settings).token is None
    _saved(settings)
    # Signed in again as another device: the old device's token is not used.
    cli.key_path(settings).unlink()
    save_key(cli.key_path(settings), "device-2", "server-1", Ed25519PrivateKey.generate())
    assert cli._client(settings).token is None
    assert not cli.session_path(settings).exists()
    cli._CLIENTS.clear()


def test_a_refused_saved_token_signs_in_once_with_the_key(tmp_path, monkeypatch):
    from job_server.client import ClientError

    settings = _signed_in(tmp_path)
    _saved(settings)
    client = cli._client(settings)
    seen = []

    def request(method, path, body=None):
        seen.append(client.token)
        if client.token == "saved-token":
            raise ClientError(401, {"message": "Authentication required"})
        return {"ok": True}

    monkeypatch.setattr(client, "request", request)
    assert cli._request(client, "GET", "/me") == {"ok": True}
    assert seen == ["saved-token", None] and not client.cached_session
    cli._CLIENTS.clear()
