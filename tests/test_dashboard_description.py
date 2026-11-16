"""The dashboard's This PC description: edited in Settings, shown in the banner."""

import pytest


@pytest.fixture
def dashboard(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from job_server.settings import Settings
    from job_server.ui.dashboard import Dashboard

    app = QApplication.instance() or QApplication([])
    config = tmp_path / "settings.json"
    Settings(
        data_dir=str(tmp_path), cpus=8, memory_mb=16384,
        description="GPU workstation in Lab 3",
    ).save(config)
    dialog = Dashboard(config=config)
    dialog.timer.stop()
    yield dialog
    dialog.close()
    app.processEvents()


def test_settings_tab_edits_and_saves_the_description(dashboard):
    from job_server.settings import Settings

    assert dashboard.description_edit.text() == "GPU workstation in Lab 3"
    dashboard.description_edit.setText("Shared DFT box - ask before big jobs")
    assert dashboard.save_settings()
    assert Settings.load(dashboard.config).description == "Shared DFT box - ask before big jobs"


def test_description_placeholder_suggests_wording_with_this_pcs_budget(dashboard):
    placeholder = dashboard.description_edit.placeholderText()
    assert "shared job server" in placeholder
    assert "8 cores" in placeholder
    assert "16.0 GB" in placeholder


def test_banner_shows_the_fixed_line_and_the_owners_note(dashboard):
    from job_server.settings import pc_description

    dashboard.set_status("unknown", "Checking", "hint")
    text = dashboard.description_label.text()
    assert pc_description() in text
    assert "GPU workstation in Lab 3" in text


def test_banner_without_a_note_shows_only_the_fixed_line(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from job_server.settings import Settings, pc_description
    from job_server.ui.dashboard import Dashboard

    app = QApplication.instance() or QApplication([])
    config = tmp_path / "settings.json"
    Settings(data_dir=str(tmp_path)).save(config)
    dialog = Dashboard(config=config)
    dialog.timer.stop()
    try:
        dialog.set_status("unknown", "Checking", "hint")
        assert dialog.description_label.text() == pc_description()
    finally:
        dialog.close()
        app.processEvents()
