"""Read-only access to an archive for other tools: no writes, no lock, finished manifests only.

Downstream tools (search, note pipelines) use this module instead of re-implementing the
data contract (docs/data-contract.md section 4). It never creates or changes a file below
the archive root, so it can run while `capture` writes: a manifest is visible only after it
is finalized, and a finalized manifest and its stored versions never change.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard

from . import APPLICATION_ID
from .archive import ArchiveIndex, ArchiveRoot, VersionRef
from .content import GZIP_MAGIC, ZSTD_MAGIC
from .fsutil import ArchiveError, os_path, path_is_within

__all__ = ["ArchiveError", "ArchiveVersion", "ArchiveView", "is_archive_root"]


@dataclass(frozen=True)
class ArchiveVersion:
    """One stored version of a relative path, as a finished manifest records it."""

    relative_path: str
    stored_path: str
    sha256: str
    byte_count: int
    captured_at: str
    run_id: str
    record_id: str
    content_encoding: str
    content_sha256: str | None
    content_byte_count: int | None
    previous_sha256: str | None
    previous_relation: str | None
    record: dict[str, Any]

    @classmethod
    def from_ref(cls, ref: VersionRef) -> ArchiveVersion:
        raw_content, raw_previous = ref.record.get("content"), ref.record.get("previous")
        content: dict[str, Any] = raw_content if isinstance(raw_content, dict) else {}
        previous: dict[str, Any] = raw_previous if isinstance(raw_previous, dict) else {}
        return cls(
            relative_path=ref.relative_path,
            stored_path=ref.stored_path,
            sha256=ref.sha256,
            byte_count=ref.byte_count,
            captured_at=ref.captured_at,
            run_id=ref.run_id,
            record_id=ref.record_id,
            content_encoding=str(content.get("encoding") or "identity"),
            content_sha256=content.get("sha256") if isinstance(content.get("sha256"), str) else None,
            content_byte_count=content.get("byteCount") if isinstance(content.get("byteCount"), int) else None,
            previous_sha256=previous.get("sha256") if isinstance(previous.get("sha256"), str) else None,
            previous_relation=previous.get("relation") if isinstance(previous.get("relation"), str) else None,
            record=dict(ref.record),
        )

    @property
    def identity_sha256(self) -> str:
        """The decoded content's SHA-256 (the stored bytes' when the content was not recorded)."""
        return self.content_sha256 or self.sha256


class ArchiveView:
    """A finished-manifest view of one archive root.

    `profile` and `source_id`, when given, must match the ownership marker. The view is a
    snapshot taken when it is created; create a new one to see later runs.
    """

    def __init__(self, root: Path | str, *, profile: str | None = None, source_id: str | None = None) -> None:
        self.root = Path(root)
        archive = ArchiveRoot(self.root)
        if not archive.exists():
            raise ArchiveError(f"archive root not found: {self.root}")
        marker = archive.read_marker()
        if marker is None:
            raise ArchiveError(f"not a {APPLICATION_ID} archive (no ownership marker): {self.root}")
        if str(marker.get("schemaVersion", "")).split(".")[0] != "1" or marker.get("rootKind") != "chatLogArchive":
            raise ArchiveError(f"unsupported archive marker: {archive.marker_path}")
        for key, expected in (("profile", profile), ("sourceId", source_id)):
            if expected is not None and marker.get(key) != expected:
                raise ArchiveError(f"archive belongs to {key}={marker.get(key)!r}, not {expected!r}: {self.root}")
        self.marker: dict[str, Any] = marker
        self._index: ArchiveIndex = archive.load_index()

    @property
    def profile(self) -> str:
        return str(self.marker.get("profile") or "")

    @property
    def source_id(self) -> str:
        return str(self.marker.get("sourceId") or "")

    @property
    def software(self) -> str:
        return str(self.marker.get("software") or "")

    @property
    def environment(self) -> str:
        return str(self.marker.get("environment") or "")

    @property
    def device_id(self) -> str:
        return str(self.marker.get("deviceId") or "")

    @property
    def manifest_count(self) -> int:
        return len(self._index.manifest_files)

    @property
    def ignored_manifests(self) -> list[dict[str, str]]:
        """Unfinished (`.partial`) and unfinalized manifests, which readers must not use."""
        partial = [{"path": str(path), "reason": "partial"} for path in self._index.partial_files]
        return partial + [dict(item) for item in self._index.invalid_files]

    @property
    def latest_run_id(self) -> str | None:
        return max(self._index.run_ids, default=None)

    def relative_paths(self, prefix: str = "") -> list[str]:
        return sorted(
            path for path, history in self._index.paths.items() if history.versions and path.startswith(prefix)
        )

    def versions(self, relative_path: str) -> list[ArchiveVersion]:
        """Versions ordered by capturedAt, then runId; the latest is last."""
        history = self._index.paths.get(relative_path)
        return [ArchiveVersion.from_ref(ref) for ref in history.versions] if history else []

    def latest(self, relative_path: str) -> ArchiveVersion | None:
        versions = self.versions(relative_path)
        return versions[-1] if versions else None

    def is_missing_at_source(self, relative_path: str) -> bool:
        history = self._index.paths.get(relative_path)
        return bool(history and history.missing)

    def stored_file(self, version: ArchiveVersion) -> Path:
        """The stored file below the archive root; any path that could leave the root is refused."""
        stored = version.stored_path
        parts = stored.split("/")
        if (
            not stored
            or stored.startswith("/")
            or "\\" in stored
            or ":" in parts[0]
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ArchiveError(f"unsafe stored path: {stored}")
        path = self.root.joinpath(*parts)
        if path == self.root or not path_is_within(path, self.root):
            raise ArchiveError(f"unsafe stored path: {stored}")
        return path

    def read_stored(self, version: ArchiveVersion) -> bytes:
        """The stored bytes, after checking their size and SHA-256 against the manifest."""
        path = self.stored_file(version)
        try:
            with open(os_path(path), "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise ArchiveError(f"cannot read stored version {version.stored_path}: {exc}") from exc
        if len(data) != version.byte_count or hashlib.sha256(data).hexdigest() != version.sha256:
            raise ArchiveError(f"stored version does not match its manifest record: {version.stored_path}")
        return data

    def read_content(self, version: ArchiveVersion) -> bytes:
        """The decoded content (zstd and gzip are expanded), checked against the recorded content hash."""
        data = self.read_stored(version)
        try:
            if data.startswith(ZSTD_MAGIC):
                data = _decode_zstd_frames(data)
            elif data.startswith(GZIP_MAGIC):
                data = gzip.decompress(data)
        except (zstandard.ZstdError, zlib.error, OSError, EOFError) as exc:
            raise ArchiveError(f"cannot decode stored version {version.stored_path}: {exc}") from exc
        if version.content_sha256 is not None and (
            hashlib.sha256(data).hexdigest() != version.content_sha256
            or (version.content_byte_count is not None and len(data) != version.content_byte_count)
        ):
            raise ArchiveError(f"decoded content does not match its manifest record: {version.stored_path}")
        return data


def _decode_zstd_frames(data: bytes) -> bytes:
    """Decode every concatenated frame; a frame that does not end is truncated data, not content."""
    parts: list[bytes] = []
    remaining = data
    decompressor = zstandard.ZstdDecompressor()
    while remaining:
        frame = decompressor.decompressobj()
        parts.append(frame.decompress(remaining))
        if not frame.eof:
            raise EOFError("incomplete zstd frame (truncated data)")
        remaining = frame.unused_data
    return b"".join(parts)


def is_archive_root(path: Path | str) -> bool:
    """Whether a folder carries this application's ownership marker (no other check)."""
    return os.path.isfile(os_path(Path(path) / ".tkn-genai-chat-archive.json"))
