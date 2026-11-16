"""`python -m job_server gui`: the dashboard without MoleditPy."""

import sys

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
