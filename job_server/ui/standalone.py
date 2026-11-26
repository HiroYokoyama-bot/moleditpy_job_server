"""The dashboard run without MoleditPy: ``python -m job_server gui``.

Like Job Manager's standalone monitor, a second launch does not open a
second window: it asks the dashboard already running for this
configuration to come to the front, and exits. The daemon is not involved
either way — this window is only a client of it — but two settings
editors racing on one settings file would still confuse, and one window
is what the user asked for when they clicked again.

Qt is imported only inside the functions, so importing this module (and
the CLI's argument parsing) stays free of Qt.
"""

import hashlib
import sys
from pathlib import Path


def instance_name(config=None):
    """The single-instance name for one configuration file.

    Keyed by the resolved config path, so dashboards for two different
    configurations can run side by side while a second launch for the
    same one raises the first.
    """
    if config:
        path = Path(config).expanduser().resolve()
    else:
        path = Path.home() / ".moleditpy" / "job_server" / "settings.json"
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return f"moleditpy-job-server-gui-{digest}"


def ask_running_to_show(name):
    """Ask the dashboard listening on ``name`` to come to the front."""
    from PySide6.QtNetwork import QLocalSocket

    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(300):
        return False
    socket.write(b"show\n")
    socket.flush()
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


def run_gui(config=None):
    """Open the dashboard as its own program. Returns the exit code."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtNetwork import QLocalServer
        from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    except ImportError:
        print(
            "The Job Server window needs PySide6. Install it with: "
            'python -m pip install "moleditpy-job-server[gui]"',
            file=sys.stderr,
        )
        return 1
    app = QApplication.instance() or QApplication(sys.argv)
    name = instance_name(config)
    if ask_running_to_show(name):
        return 0
    # A window that was killed leaves its name behind; clear it and listen.
    QLocalServer.removeServer(name)
    server = QLocalServer()
    if not server.listen(name):
        server = None  # another window won the race; open ours anyway

    from ..plugin import dependency_dialog, missing_dependencies

    missing = missing_dependencies()
    if missing:
        dependency_dialog(None, missing)
        return 1
    try:
        from .dashboard import Dashboard

        dialog = Dashboard(config=config)
    except ImportError as exc:
        dependency_dialog(None, [exc.name or str(exc)])
        return 1

    def show_dashboard():
        dialog.setWindowState(dialog.windowState() & ~Qt.WindowState.WindowMinimized)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    if sys.platform in ("win32", "linux") and QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(dialog.windowIcon(), dialog)
        tray.setToolTip("MoleditPy Job Server")
        menu = QMenu(dialog)
        menu.addAction("Show dashboard", show_dashboard)
        menu.addAction("Close dashboard", dialog.close)
        tray.setContextMenu(menu)

        def on_tray_activated(reason):
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                show_dashboard()

        tray.activated.connect(on_tray_activated)
        dialog.finished.connect(tray.hide)
        tray.show()

    if server is not None:
        pending = []

        def on_connection():
            connection = server.nextPendingConnection()
            if connection is None:
                return
            pending.append(connection)

            def forget():
                if connection in pending:
                    pending.remove(connection)

            connection.disconnected.connect(forget)
            show_dashboard()

        server.newConnection.connect(on_connection)
    dialog.show()
    return app.exec()
