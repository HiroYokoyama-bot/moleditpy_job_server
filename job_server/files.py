import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from .errors import reject


RESERVED = {
    ".moleditpy_rc",
    ".moleditpy_rc.tmp",
    "moleditpy_run.sh",
    "moleditpy_run.ps1",
    "job.log",
    "payload.ps1",
    ".server_process",
}
#: Reserved names an owner may still read from the job folder: the job's log and
#: the script that ran it. Nothing may upload over them, but downloading them
#: shows the owner nothing a command in their own job could not already print.
READABLE = {"job.log", "moleditpy_run.sh", "moleditpy_run.ps1", "payload.ps1"}
DEVICES = re.compile(r"^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.I)


def atomic_write(path, data):
    """Replace a private file through an exclusively created sibling, never a fixed .tmp."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def is_link(path):
    """Include Windows junction/reparse points on Python 3.11 as well as symlinks."""
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _reserved(part, readable):
    name = part.casefold()
    return name in RESERVED and not (readable and name in READABLE)


def validate_path(value, readable=False):
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value:
        reject(400, "invalid_path", "Use a relative slash-separated path")
    parts = value.split("/")
    for part in parts:
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or len(part) > 255
            or any(ord(c) < 32 or c in ':<>"|?*' for c in part)
            or DEVICES.match(part)
            or _reserved(part, readable)
        ):
            reject(400, "invalid_path", "Unsafe or reserved file path")
    return value


def contained(root, relative, create=False, readable=False):
    """``readable`` lets the job's log and script through, for reading only."""
    if create and readable:
        raise ValueError("A readable-only path cannot be created")
    root = Path(root)
    validate_path(relative, readable)
    if is_link(root):
        reject(400, "unsafe_path", "Linked job root is forbidden")
    target = root / relative
    current = root
    for part in relative.split("/"):
        current = current / part
        if is_link(current):
            reject(400, "unsafe_path", "Links and junctions are forbidden")
    resolved, base = target.resolve(), root.resolve()
    if not resolved.is_relative_to(base):
        reject(400, "unsafe_path", "File escapes job directory")
    # A Windows 8.3 short name (MOLEDI~1.PS1) reaches a file under a name the
    # check above never saw, so judge the name the file really has.
    if any(_reserved(part, readable) for part in resolved.relative_to(base).parts):
        reject(400, "invalid_path", "Unsafe or reserved file path")
    if create:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return target


def open_regular(path, mode="rb"):
    flags = os.O_RDONLY if mode == "rb" else os.O_RDWR | os.O_CREAT
    flags |= (
        getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    if is_link(path):
        reject(400, "unsafe_file", "Linked files are forbidden")
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError:
        reject(404, "file_missing", "File not found")
    except OSError:
        reject(400, "unsafe_file", "Cannot open file safely")
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        reject(400, "unsafe_file", "Only ordinary unlinked files can be transferred")
    return os.fdopen(descriptor, mode)


def sha256(path):
    digest = hashlib.sha256()
    with open_regular(path) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def disk_usage(root):
    """Total size of the regular files under a folder, in bytes.

    Stat-only (no file is opened or read), links are not followed, and a
    file that vanishes mid-walk is skipped: this runs on folders other
    processes may still be writing.
    """
    total = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not is_link(Path(directory) / name)]
        for name in names:
            path = Path(directory) / name
            if is_link(path):
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _listed(root):
    """(relative path, path, stat) for every file a job folder lists, in order.

    Only ordinary, singly-linked files inside the folder itself: the same
    set the download endpoints will serve.
    """
    from .errors import ServerError

    found = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not is_link(Path(directory) / name)]
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if any(part.casefold() in RESERVED for part in relative.split("/")):
                continue
            try:
                path = contained(root, relative)
                info = path.stat()
            except (ServerError, OSError):
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                found.append((relative, path, info))
    return sorted(found, key=lambda item: item[0])


def listing(root):
    """A folder's files as path and size only, from a stat-only walk that
    never reads a file: for views that show the list first and fill the
    digests in once they are recorded."""
    return [
        {"path": relative, "size": info.st_size}
        for relative, _path, info in _listed(root)
    ]


def manifest_stamp(root):
    """A folder's listed files as (path, size, mtime) rows: a cheap stat-only
    fingerprint that changes whenever a listed file is added, removed or
    rewritten, so a stored manifest can be told apart from a stale one."""
    return [[relative, info.st_size, info.st_mtime_ns] for relative, _path, info in _listed(root)]


def manifest_record(root, previous=None):
    """A folder's manifest with digests, plus its stamp, in one walk.

    A file unchanged since the previous record (same size and mtime) keeps
    its digest, so refreshing a growing job folder only reads the files
    that were added or rewritten since the last record.
    """
    from .errors import ServerError

    known = {}
    if previous:
        for row, entry in zip(previous["stamp"], previous["entries"]):
            known[row[0]] = (row[1], row[2], entry["sha256"])
    entries, stamp = [], []
    for relative, path, info in _listed(root):
        prior = known.get(relative)
        if prior and prior[0] == info.st_size and prior[1] == info.st_mtime_ns:
            digest = prior[2]
        else:
            try:
                digest = sha256(path)
            except (ServerError, OSError):
                continue
        entries.append({"path": relative, "size": info.st_size, "sha256": digest})
        stamp.append([relative, info.st_size, info.st_mtime_ns])
    return {"entries": entries, "stamp": stamp}
