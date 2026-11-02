"""File system helpers: long paths, no-replace placement, durable writes, timestamps."""

from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt"
CHUNK_SIZE = 1024 * 1024


class ArchiveError(RuntimeError):
    """A condition that stops work on one target without corrupting stored data."""


def os_path(path: Path | str) -> str:
    """Return a path string that works beyond MAX_PATH on Windows."""

    text = os.fspath(path)
    if not IS_WINDOWS or text.startswith("\\\\?\\"):
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def plain_path(path: Path | str) -> str:
    """Return a path for display, without the Windows extended-length prefix."""

    text = os.fspath(path)
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso_seconds(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def iso_from_ns(timestamp_ns: int) -> str:
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000).astimezone().isoformat(timespec="milliseconds")


def run_id_for(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S%z")


def is_link(path: Path | str) -> bool:
    text = os_path(path)
    try:
        info = os.lstat(text)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse = getattr(info, "st_reparse_tag", 0)
    # Junctions and other name-surrogate reparse points are links; OneDrive placeholders are not.
    return bool(reparse and reparse & 0x20000000)


def rename_no_replace(source: str, destination: str) -> None:
    """Move a file into place, failing instead of replacing an existing destination."""

    if IS_WINDOWS:
        os.rename(source, destination)
    else:
        os.link(source, destination)
        os.unlink(source)


def temporary_sibling(path: Path) -> Path:
    return path.parent / f".~tkn-{uuid.uuid4().hex[:12]}.tmp"


def write_new_file(path: Path, content: bytes) -> None:
    """Create a file with complete content; never replaces an existing file."""

    os.makedirs(os_path(path.parent), exist_ok=True)
    temporary = temporary_sibling(path)
    try:
        with open(os_path(temporary), "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        rename_no_replace(os_path(temporary), os_path(path))
    finally:
        if os.path.exists(os_path(temporary)):
            os.unlink(os_path(temporary))


def replace_file(path: Path, content: bytes) -> None:
    """Atomically replace a state or report file (never used for archive versions)."""

    os.makedirs(os_path(path.parent), exist_ok=True)
    temporary = temporary_sibling(path)
    try:
        with open(os_path(temporary), "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(os_path(temporary), os_path(path))
    finally:
        if os.path.exists(os_path(temporary)):
            os.unlink(os_path(temporary))


def json_bytes(value: Any, *, indent: int | None = 2) -> bytes:
    text = json.dumps(value, ensure_ascii=False, indent=indent)
    return (text + "\n").encode("utf-8")


def json_line(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def set_read_only(path: Path | str) -> None:
    os.chmod(os_path(path), stat.S_IREAD)


def path_is_within(child: Path, parent: Path) -> bool:
    child_text = os.path.normcase(os.path.abspath(child))
    parent_text = os.path.normcase(os.path.abspath(parent))
    return child_text == parent_text or child_text.startswith(parent_text.rstrip("\\/") + os.sep)


def resolved(path: Path) -> Path:
    try:
        return Path(os.path.realpath(path))
    except OSError:
        return Path(os.path.abspath(path))
