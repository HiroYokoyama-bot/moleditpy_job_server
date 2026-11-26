"""`python -m job_server gui`: the dashboard without MoleditPy."""

import sys
from types import SimpleNamespace

import pytest

from job_server.__main__ import main


def test_gui_dispatches_to_the_standalone_launcher(tmp_path, monkeypatch):
    seen = {}

    def fake_run_gui(config):
        seen["config"] = config
        return 7

    monkeypatch.setattr("job_server.ui.standalone.run_gui", fake_run_gui)
    config = tmp_path / "settings.json"
    monkeypatch.setattr(sys, "argv", ["job-server", "gui", "--config", str(config)])
    assert main() == 7
    assert seen["config"] == config


def test_instance_names_are_stable_and_per_configuration(tmp_path):
    from job_server.ui.standalone import instance_name

    one = tmp_path / "one" / "settings.json"
    two = tmp_path / "two" / "settings.json"
    assert instance_name(one) == instance_name(one)
    assert instance_name(one) != instance_name(two)
    assert instance_name() != instance_name(one)


def test_ask_running_to_show_reaches_a_listening_dashboard(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication
    from job_server.ui.standalone import ask_running_to_show, instance_name

    app = QApplication.instance() or QApplication([])
    name = instance_name(tmp_path / "settings.json")
    QLocalServer.removeServer(name)
    server = QLocalServer()
    assert server.listen(name)
    try:
        assert ask_running_to_show(name)
        app.processEvents()
        assert server.hasPendingConnections()
    finally:
        server.close()
        QLocalServer.removeServer(name)
    assert not ask_running_to_show(name)


@pytest.mark.parametrize("tray_available", [True, False])
def test_standalone_dashboard_tray(tmp_path, monkeypatch, tray_available):
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QIcon, QPixmap
    from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon
    from job_server.ui import standalone

    app = QApplication.instance() or QApplication([])
    dashboard = QDialog()
    dashboard.setWindowIcon(QIcon(QPixmap(16, 16)))
    monkeypatch.setattr(standalone, "sys", SimpleNamespace(platform="linux", argv=[]))
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: tray_available)
    monkeypatch.setattr("job_server.plugin.missing_dependencies", lambda: [])
    monkeypatch.setattr("job_server.ui.dashboard.Dashboard", lambda config: dashboard)

    def check_dashboard():
        assert dashboard.isVisible()
        tray = dashboard.findChild(QSystemTrayIcon)
        if not tray_available:
            assert tray is None
            dashboard.close()
            return 0

        assert tray.isVisible()
        assert tray.icon().cacheKey() == dashboard.windowIcon().cacheKey()
        show_action, close_action = tray.contextMenu().actions()
        dashboard.showMinimized()
        show_action.trigger()
        assert not dashboard.isMinimized()
        dashboard.showMinimized()
        tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        assert not dashboard.isMinimized()
        close_action.trigger()
        assert not dashboard.isVisible()
        assert not tray.isVisible()
        return 0

    monkeypatch.setattr(app, "exec", check_dashboard)
    try:
        assert standalone.run_gui(tmp_path / "settings.json") == 0
    finally:
        dashboard.close()
