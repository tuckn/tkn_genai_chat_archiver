"""Archive root ownership, locking, manifests and the version index."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from . import APPLICATION_ID
from .config import TargetConfig
from .fsutil import ArchiveError, json_bytes, json_line, os_path, rename_no_replace, set_read_only, write_new_file

MARKER_NAME = ".tkn-genai-chat-archive.json"
LOCK_NAME = ".pipeline.lock"
MANIFESTS_DIR = "manifests"
SNAPSHOTS_DIR = "snapshots"
MANIFEST_SCHEMA_VERSION = "1.0.0"
MARKER_SCHEMA_VERSION = "1.0.0"
LEGACY_MARKER_NAME = ".tkn-genai-chat-note-root.json"
LEGACY_MANIFEST_NAME = "manifest.jsonl"
LEGACY_APPLICATION_ID = "tkn-genai-chat-note-pipeline"
RUN_ID_PATTERN = re.compile(r"^(\d{8}T\d{6}[+-]\d{4})(?:-(\d+))?$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def run_sort_key(file_name: str) -> tuple[str, int, str]:
    """Chronological order of manifests; `<id>-2` follows `<id>` although '-' sorts before '.'."""

    run_id = file_name.split(".", 1)[0]
    match = RUN_ID_PATTERN.fullmatch(run_id)
    if not match:
        return (run_id, 0, file_name)
    return (match.group(1), int(match.group(2) or 1), file_name)


def sha256_file(path: Path | str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(os_path(path), "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


@dataclass
class VersionRef:
    record_id: str
    relative_path: str
    stored_path: str
    sha256: str
    byte_count: int
    captured_at: str
    run_id: str
    record: dict[str, Any]


@dataclass
class PathHistory:
    versions: list[VersionRef] = field(default_factory=list)
    missing: bool = False

    @property
    def latest(self) -> VersionRef | None:
        return self.versions[-1] if self.versions else None


@dataclass
class ArchiveIndex:
    paths: dict[str, PathHistory] = field(default_factory=dict)
    pairs: set[tuple[str, str]] = field(default_factory=set)
    run_ids: set[str] = field(default_factory=set)
    manifest_files: list[Path] = field(default_factory=list)
    partial_files: list[Path] = field(default_factory=list)
    invalid_files: list[dict[str, str]] = field(default_factory=list)

    def add(self, record: dict[str, Any]) -> None:
        kind = record.get("recordType")
        relative = str(record.get("relativePath") or "")
        if kind == "version":
            ref = VersionRef(
                record_id=str(record["recordId"]),
                relative_path=relative,
                stored_path=str(record["storedPath"]),
                sha256=str(record["sha256"]),
                byte_count=int(record["byteCount"]),
                captured_at=str(record.get("capturedAt") or ""),
                run_id=str(record["runId"]),
                record=record,
            )
            history = self.paths.setdefault(relative, PathHistory())
            history.versions.append(ref)
            history.missing = False
            self.pairs.add((relative, ref.sha256))
        elif kind == "sourceMissing":
            self.paths.setdefault(relative, PathHistory()).missing = True

    @property
    def version_count(self) -> int:
        return sum(len(item.versions) for item in self.paths.values())

    @property
    def stored_bytes(self) -> int:
        return sum(version.byte_count for item in self.paths.values() for version in item.versions)


def _validate_record(record: Any, where: str) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("schemaVersion", "").split(".")[0] != "1":
        raise ArchiveError(f"unsupported manifest record: {where}")
    kind = record.get("recordType")
    if kind == "version":
        stored = str(record.get("storedPath") or "")
        if (
            not record.get("recordId")
            or not record.get("relativePath")
            or not stored
            or stored.startswith("/")
            or ".." in stored.split("/")
            or not SHA256_PATTERN.fullmatch(str(record.get("sha256") or ""))
            or not isinstance(record.get("byteCount"), int)
        ):
            raise ArchiveError(f"invalid version record: {where}")
    elif kind not in {"sourceMissing", "run"}:
        raise ArchiveError(f"unknown manifest record type {kind!r}: {where}")
    return record


def read_manifest_lines(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Return records and whether the last line was a truncated write."""

    records: list[dict[str, Any]] = []
    truncated = False
    with open(os_path(path), "rb") as handle:
        lines = handle.read().split(b"\n")
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if number == len(lines) or (number == len(lines) - 1 and not lines[-1].strip()):
                truncated = True
                break
            raise ArchiveError(f"invalid manifest JSON at {path}:{number}: {exc}") from exc
        records.append(_validate_record(value, f"{path}:{number}"))
    return records, truncated


class ArchiveRoot:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def marker_path(self) -> Path:
        return self.root / MARKER_NAME

    @property
    def manifests_dir(self) -> Path:
        return self.root / MANIFESTS_DIR

    @property
    def snapshots_dir(self) -> Path:
        return self.root / SNAPSHOTS_DIR

    def exists(self) -> bool:
        return os.path.isdir(os_path(self.root))

    def read_marker(self) -> dict[str, Any] | None:
        if not os.path.isfile(os_path(self.marker_path)):
            return None
        try:
            with open(os_path(self.marker_path), encoding="utf-8-sig") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ArchiveError(f"invalid archive marker: {self.marker_path}: {exc}") from exc
        if not isinstance(value, dict) or value.get("applicationId") != APPLICATION_ID:
            raise ArchiveError(f"archive marker belongs to another application: {self.marker_path}")
        return value

    def entries(self) -> list[str]:
        if not self.exists():
            return []
        return sorted(os.listdir(os_path(self.root)))

    def state(self) -> str:
        """missing, empty, owned, legacy or foreign."""

        if not self.exists():
            return "missing"
        names = self.entries()
        if MARKER_NAME in names:
            return "owned"
        if LEGACY_MARKER_NAME in names:
            return "legacy"
        if not names:
            return "empty"
        return "foreign"

    def check_identity(self, target: TargetConfig, marker: dict[str, Any]) -> None:
        for key, expected in (("profile", target.profile.name), ("sourceId", target.source_id)):
            if marker.get(key) != expected:
                raise ArchiveError(f"archive root belongs to {key}={marker.get(key)!r}, not {expected!r}: {self.root}")

    def check_legacy_guard(self, marker: dict[str, Any]) -> None:
        legacy = marker.get("legacyStore")
        if not legacy:
            return
        for name, key in ((LEGACY_MANIFEST_NAME, "manifestSha256"), (LEGACY_MARKER_NAME, "markerSha256")):
            path = self.root / name
            if not os.path.isfile(os_path(path)):
                raise ArchiveError(f"legacy store file disappeared after adoption: {path}")
            digest, _size = sha256_file(path)
            if digest != legacy.get(key):
                raise ArchiveError(
                    f"legacy store file changed after adoption (another program wrote to this archive): {path}"
                )

    def new_marker(self, target: TargetConfig, created_at: str, legacy: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "schemaVersion": MARKER_SCHEMA_VERSION,
            "applicationId": APPLICATION_ID,
            "rootKind": "chatLogArchive",
            "profile": target.profile.name,
            "software": target.profile.software,
            "sourceId": target.source_id,
            "deviceId": target.device_id,
            "environment": target.environment,
            "createdAt": created_at,
            "legacyStore": legacy,
        }

    def write_marker(self, marker: dict[str, Any], *, read_only: bool) -> None:
        write_new_file(self.marker_path, json_bytes(marker))
        if read_only:
            set_read_only(self.marker_path)

    @contextmanager
    def lock(self) -> Iterator[None]:
        path = self.root / LOCK_NAME
        with open(os_path(path), "a+b") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ArchiveError(f"another process holds the archive lock: {path}") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load_index(self) -> ArchiveIndex:
        index = ArchiveIndex()
        directory = self.manifests_dir
        if not os.path.isdir(os_path(directory)):
            return index
        names = sorted(os.listdir(os_path(directory)), key=run_sort_key)
        for name in names:
            path = directory / name
            if name.endswith(".jsonl.partial"):
                index.partial_files.append(path)
                continue
            if not name.endswith(".jsonl"):
                continue
            records, truncated = read_manifest_lines(path)
            if truncated or not records or records[-1].get("recordType") != "run":
                index.invalid_files.append({"path": str(path), "reason": "manifest is not finalized with a run record"})
                continue
            index.manifest_files.append(path)
            index.run_ids.add(name[: -len(".jsonl")])
            for record in records:
                index.add(record)
        for history in index.paths.values():
            history.versions.sort(key=lambda item: (item.captured_at, item.run_id))
        return index

    def unique_run_id(self, run_id: str, index: ArchiveIndex) -> str:
        candidate = run_id
        counter = 2
        while (
            candidate in index.run_ids
            or os.path.exists(os_path(self.manifests_dir / f"{candidate}.jsonl"))
            or os.path.exists(os_path(self.manifests_dir / f"{candidate}.jsonl.partial"))
            or os.path.exists(os_path(self.snapshots_dir / candidate))
        ):
            candidate = f"{run_id}-{counter}"
            counter += 1
        return candidate


class ManifestWriter:
    """Append records to `<runId>.jsonl.partial`; finalize by renaming to `<runId>.jsonl`."""

    def __init__(self, archive: ArchiveRoot, run_id: str, *, read_only: bool) -> None:
        self.archive = archive
        self.run_id = run_id
        self.read_only = read_only
        self.partial_path = archive.manifests_dir / f"{run_id}.jsonl.partial"
        self.final_path = archive.manifests_dir / f"{run_id}.jsonl"
        self._handle: IO[bytes] | None = None
        self.record_count = 0

    def append(self, record: dict[str, Any]) -> None:
        if self._handle is None:
            os.makedirs(os_path(self.archive.manifests_dir), exist_ok=True)
            self._handle = open(os_path(self.partial_path), "xb")  # noqa: SIM115
        self._handle.write(json_line(record))
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self.record_count += 1

    @property
    def started(self) -> bool:
        return self._handle is not None

    def finalize(self, run_record: dict[str, Any]) -> Path:
        self.append(run_record)
        assert self._handle is not None
        self._handle.close()
        self._handle = None
        rename_no_replace(os_path(self.partial_path), os_path(self.final_path))
        if self.read_only:
            set_read_only(self.final_path)
        return self.final_path

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
