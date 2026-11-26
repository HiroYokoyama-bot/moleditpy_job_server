"""`python -m job_server gui`: the dashboard without MoleditPy."""

import ctypes
import sys
from pathlib import Path
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


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_standalone_dashboard_has_its_own_application_icon(tmp_path, monkeypatch, platform):
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QDialog
    from job_server.ui import dashboard as dashboard_module
    from job_server.ui import standalone

    app = QApplication.instance() or QApplication([])
    original_name, original_desktop_file = app.applicationName(), app.desktopFileName()
    original_icon = app.windowIcon()
    dashboard = QDialog()
    root = Path(__file__).resolve().parents[1]
    dashboard.setWindowIcon(QIcon(str(root / "job_server" / "web" / "icon.png")))
    startup = []

    def create_app(argv):
        startup.append("QApplication")
        return app

    def set_windows_app_id(app_id):
        startup.append(app_id)

    create_app.instance = lambda: None
    monkeypatch.setattr(QtWidgets, "QApplication", create_app)
    monkeypatch.setattr(standalone, "sys", SimpleNamespace(platform=platform, argv=[]))
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(
            shell32=SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=set_windows_app_id)
        ),
        raising=False,
    )
    monkeypatch.setattr("job_server.plugin.missing_dependencies", lambda: [])
    monkeypatch.setattr(dashboard_module, "Dashboard", lambda config: dashboard)

    def check_dashboard():
        assert dashboard.isVisible()
        assert dashboard.windowType() == Qt.WindowType.Window
        assert not app.windowIcon().isNull()
        assert app.windowIcon().cacheKey() == dashboard.windowIcon().cacheKey()
        if platform == "win32":
            assert startup == [standalone.DESKTOP_APP_ID, "QApplication"]
        else:
            app_id = standalone.DESKTOP_APP_ID
            assert app.applicationName() == app_id
            assert app.desktopFileName() == app_id
            desktop_entry = root / f"{app_id}.desktop"
            fields = desktop_entry.read_text(encoding="utf-8").splitlines()
            assert f"StartupWMClass={app_id}" in fields
            assert f"Icon={app_id}" in fields
        return 0

    monkeypatch.setattr(app, "exec", check_dashboard)
    try:
        assert standalone.run_gui(tmp_path / "settings.json") == 0
    finally:
        dashboard.close()
        app.setApplicationName(original_name)
        app.setDesktopFileName(original_desktop_file)
        app.setWindowIcon(original_icon)
