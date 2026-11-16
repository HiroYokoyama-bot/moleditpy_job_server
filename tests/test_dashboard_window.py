"""The dashboard as a window: modeless, with a full-screen mode."""

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


def test_the_dashboard_is_a_modeless_window(dashboard):
    from PySide6.QtCore import Qt

    assert not dashboard.isModal()
    flags = dashboard.windowFlags()
    assert flags & Qt.WindowType.WindowMinimizeButtonHint
    assert flags & Qt.WindowType.WindowMaximizeButtonHint


def test_fullscreen_toggles_with_the_menu_action(dashboard):
    dashboard.show()
    assert not dashboard.isFullScreen()
    dashboard.toggle_fullscreen()
    assert dashboard.isFullScreen()
    assert dashboard.fullscreen_action.isChecked()
    dashboard.toggle_fullscreen()
    assert not dashboard.isFullScreen()
    assert not dashboard.fullscreen_action.isChecked()


def test_escape_leaves_fullscreen_instead_of_closing(dashboard):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    dashboard.show()
    dashboard.toggle_fullscreen()
    assert dashboard.isFullScreen()
    dashboard.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert not dashboard.isFullScreen()
    assert dashboard.isVisible()
