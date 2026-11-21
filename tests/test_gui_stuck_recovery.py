"""The window must not freeze on its last message when a worker call misbehaves."""

import threading
import time

import pytest


@pytest.fixture
def dashboard(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from job_server.settings import Settings
    from job_server.ui.dashboard import Dashboard

    app = QApplication.instance() or QApplication([])
    config = tmp_path / "settings.json"
    Settings(data_dir=str(tmp_path)).save(config)
    dialog = Dashboard(config=config)
    dialog.timer.stop()
    yield dialog
    dialog.close()
    app.processEvents()


def settle(dashboard, until, timeout=5):
    from PySide6.QtWidgets import QApplication

    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    return until()


def flush_log():
    from job_server.ui.gui_log import LOG

    for handler in LOG.handlers:
        handler.flush()


def test_refresh_failure_is_written_to_gui_log(dashboard, monkeypatch):
    def timed_out(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(dashboard.client, "request", timed_out)
    dashboard.refresh()
    assert settle(dashboard, lambda: not dashboard.busy)
    assert "not responding" in dashboard.message.text()
    flush_log()
    text = (dashboard.settings.root / "logs" / "gui.log").read_text(encoding="utf-8")
    assert "window" in text and "opened" in text
    assert "TimeoutError: timed out" in text
    assert "Refresh failed (1 in a row)" in text


def test_stuck_task_is_abandoned_and_refresh_resumes(dashboard, monkeypatch):
    from job_server.ui import dashboard as module

    release = threading.Event()
    entered = threading.Event()

    def blocked():
        entered.set()
        release.wait(10)
        return "late"

    late = []
    dashboard.task(blocked, late.append, interactive=False)
    try:
        assert entered.wait(2)
        stuck_pool, stuck_client = dashboard.pool, dashboard.client
        monkeypatch.setattr(module, "STUCK_SECONDS", 0)

        refreshed = []
        monkeypatch.setattr(
            module.JobServerClient, "request",
            lambda self, *args, **kwargs: refreshed.append(args[1]) or (_ for _ in ()).throw(TimeoutError()),
        )
        dashboard.refresh()
        assert dashboard.pool is not stuck_pool
        assert dashboard.client is not stuck_client
        assert dashboard.client.url == stuck_client.url
        assert settle(dashboard, lambda: refreshed and not dashboard.busy)
        assert refreshed[0] == "/health"
    finally:
        release.set()
    stuck_pool.waitForDone(5000)
    settle(dashboard, lambda: False, timeout=0.2)
    assert late == []  # the abandoned call's result is ignored
    flush_log()
    text = (dashboard.settings.root / "logs" / "gui.log").read_text(encoding="utf-8")
    assert "has not returned" in text and "blocked" in text  # the worker's stack is logged


def test_queue_moves_on_when_showing_a_result_raises(dashboard):
    release = threading.Event()
    ran = []

    def first():
        release.wait(5)
        return 1

    def broken_display(_):
        raise KeyError("unexpected field")

    dashboard.task(first, broken_display)
    dashboard.task(lambda: ran.append("second") or 2, lambda _: None)
    assert dashboard._pending_tasks
    release.set()
    assert settle(dashboard, lambda: ran == ["second"] and not dashboard.busy)
    assert not dashboard._pending_tasks


def test_left_over_queue_is_started_by_refresh(dashboard, monkeypatch):
    ran = []
    dashboard._pending_tasks.append((lambda: ran.append("queued"), None, None, None, None))
    monkeypatch.setattr(dashboard.client, "request", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    dashboard.refresh()
    assert settle(dashboard, lambda: ran == ["queued"] and not dashboard.busy)


def test_task_reports_base_exceptions(dashboard, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))

    class Escaped(BaseException):
        pass

    def escapes():
        raise Escaped()

    dashboard.task(escapes)
    assert settle(dashboard, lambda: not dashboard.busy)
    assert dashboard.message.text() == "Escaped"
    assert warnings


def test_failure_text_names_the_exception():
    pytest.importorskip("PySide6")
    from job_server.ui.dashboard import failure_text

    assert failure_text(TimeoutError("timed out")) == "TimeoutError: timed out"
    assert failure_text(TimeoutError()) == "TimeoutError"
    assert failure_text(ValueError("ValueError: already named")) == "ValueError: already named"


def read_log(dashboard):
    flush_log()
    return (dashboard.settings.root / "logs" / "gui.log").read_text(encoding="utf-8")


def no_signals(monkeypatch):
    """Connect nothing, as if every queued signal from the worker were lost."""
    from PySide6.QtCore import QObject
    from job_server.ui import dashboard as module

    monkeypatch.setattr(module, "connect_queued", lambda signal, callback, owner, *a, **k: QObject(owner))


def test_lost_result_is_delivered_by_the_refresh_timer(dashboard, monkeypatch):
    from job_server.ui import dashboard as module

    no_signals(monkeypatch)
    got = []
    dashboard.task(lambda: 5, got.append, interactive=False)
    task = dashboard._current_task
    assert settle(dashboard, lambda: task.outcome is not None)
    settle(dashboard, lambda: False, timeout=0.1)
    assert got == [] and dashboard.busy  # the signal never came

    monkeypatch.setattr(module, "DELIVERY_SECONDS", 0)
    monkeypatch.setattr(dashboard.client, "request", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    dashboard.refresh()
    assert got == [5]
    assert "never reached the window" in read_log(dashboard)


def test_lost_failure_is_delivered_during_a_server_operation(dashboard, monkeypatch):
    """Start/stop/restart stop refresh, so a lost result there must still arrive."""
    from job_server.ui import dashboard as module

    no_signals(monkeypatch)
    errors = []

    def broken():
        raise ValueError("daemon did not start")

    dashboard.task(broken, interactive=False, on_error=errors.append)
    task = dashboard._current_task
    assert settle(dashboard, lambda: task.outcome is not None)
    dashboard._pending_lifecycle.add("start")
    monkeypatch.setattr(module, "DELIVERY_SECONDS", 0)
    dashboard.refresh()
    assert errors == ["daemon did not start"]
    assert not dashboard.busy


def test_result_is_delivered_once(dashboard, monkeypatch):
    from job_server.ui import dashboard as module

    got = []
    dashboard.task(lambda: 7, got.append, interactive=False)
    task = dashboard._current_task
    assert settle(dashboard, lambda: got == [7])
    # A late timer pass over the same task must not show it again.
    monkeypatch.setattr(module, "DELIVERY_SECONDS", 0)
    task.deliver["finished"](7)
    assert got == [7]


def test_call_the_pool_never_starts_runs_on_a_thread(dashboard, monkeypatch):
    from job_server.ui import dashboard as module

    pool_starts = []
    monkeypatch.setattr(dashboard.pool, "start", pool_starts.append)
    monkeypatch.setattr(module, "START_SECONDS", 0)
    got = []
    dashboard.task(lambda: "first", got.append, interactive=False)
    assert len(pool_starts) == 1
    monkeypatch.setattr(dashboard.client, "request", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    dashboard._watch_current_task()
    assert dashboard._own_threads
    assert settle(dashboard, lambda: got == ["first"] and not dashboard.busy)
    # Later calls go straight to their own thread.
    dashboard.task(lambda: "second", got.append, interactive=False)
    assert settle(dashboard, lambda: got == ["first", "second"] and not dashboard.busy)
    assert len(pool_starts) == 1
    assert "was not started by the worker pool" in read_log(dashboard)


def test_a_call_runs_once_even_if_two_threads_try(dashboard):
    from job_server.ui.dashboard import Task

    calls = []
    task = Task(lambda: calls.append(1))
    task.run()
    task.run()
    assert calls == [1]


def test_thread_start_failure_is_reported(dashboard, monkeypatch):
    errors = []
    dashboard._own_threads = True

    def cannot_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    dashboard.task(lambda: None, interactive=False, on_error=errors.append)
    assert errors and "could not start a worker thread" in errors[0]
    assert not dashboard.busy


def test_never_started_call_is_named_in_the_log(dashboard, monkeypatch):
    from job_server.ui import dashboard as module

    monkeypatch.setattr(dashboard.pool, "start", lambda task: None)
    monkeypatch.setattr(module, "START_SECONDS", 10_000)
    monkeypatch.setattr(module, "STUCK_SECONDS", 0)
    dashboard.task(lambda: None, interactive=False)
    dashboard._recover_stuck_task()
    assert not dashboard.busy
    assert "it never started on a worker thread" in read_log(dashboard)
