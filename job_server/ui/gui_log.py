# SPDX-License-Identifier: LGPL-3.0-or-later
"""The dashboard window's own log, beside the daemon's in the logs folder.

The window runs under pythonw on Windows, where there is no console: without
a file, a refresh that fails or a request that never returns leaves no trace
anywhere. Records still propagate, so MoleditPy's own logging sees them too.
"""

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG = logging.getLogger("job_server.gui")
LOG.setLevel(logging.INFO)

#: Size of gui.log before it rolls over, and how many old copies are kept.
MAX_BYTES = 1024 * 1024
BACKUPS = 2


def attach(directory):
    """Write the window's records to ``directory/gui.log``.

    One file at a time: a window opened for another data folder takes the log
    over, and the previous file is closed rather than held open.
    """
    path = (Path(directory) / "gui.log").resolve()
    for handler in list(LOG.handlers):
        if not isinstance(handler, RotatingFileHandler):
            continue
        if Path(handler.baseFilename) == path:
            return handler
        LOG.removeHandler(handler)
        handler.close()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True
        )
    except OSError:
        return None
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    LOG.addHandler(handler)
    return handler


def thread_stack(ident):
    """The current stack of the thread ``ident``, or a note that it has none."""
    frame = sys._current_frames().get(ident) if ident else None
    if frame is None:
        return "(the worker thread has no Python frame)"
    return "".join(traceback.format_stack(frame)).rstrip()
