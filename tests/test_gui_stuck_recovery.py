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
