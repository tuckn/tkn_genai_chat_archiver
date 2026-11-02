"""Adopt a Raw store written by tkn-codex-chat-note-pipeline without changing any of its files."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from . import APPLICATION_ID, __version__
from .archive import (
    LEGACY_APPLICATION_ID,
    LEGACY_MANIFEST_NAME,
    LEGACY_MARKER_NAME,
    LOCK_NAME,
    MANIFEST_SCHEMA_VERSION,
    MANIFESTS_DIR,
    ArchiveRoot,
    ManifestWriter,
    read_manifest_lines,
    sha256_file,
)
from .config import TargetConfig
from .content import StreamDigest
from .fsutil import CHUNK_SIZE, ArchiveError, iso_from_ns, iso_seconds, now_local, os_path

LOGGER = logging.getLogger("tkn_genai_chat_archiver")
LEGACY_ALLOWED_ENTRIES = {
    LEGACY_MARKER_NAME,
    LOCK_NAME,
    LEGACY_MANIFEST_NAME,
    "sessions",
    "archived_sessions",
    "metadata",
}
LEGACY_TREES = ("sessions", "archived_sessions")
METADATA_NAME = re.compile(r"^([0-9a-f]{64})\.json$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
APP_STATE_RELATIVE_PATH = ".codex-global-state.json"


def _digest(path: Path) -> StreamDigest:
    digest = StreamDigest()
    with open(os_path(path), "rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest


def _read_json(path: Path) -> Any:
    with open(os_path(path), encoding="utf-8-sig") as handle:
        return json.load(handle)


def _legacy_records(root: Path, source_id: str, problems: list[str]) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
    seen: set[str] = set()
    with open(os_path(root / LEGACY_MANIFEST_NAME), encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"{LEGACY_MANIFEST_NAME}:{number}: invalid JSON: {exc}")
                continue
            reference = str(value.get("sourceRef") or "") if isinstance(value, dict) else ""
            parts = reference.split("/")
            if (
                not isinstance(value, dict)
                or value.get("schemaVersion") != 3
                or value.get("sourceId") != source_id
                or value.get("sourceProvider") != "codex"
                or parts[0] not in LEGACY_TREES
                or any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in parts)
                or not SHA256.fullmatch(str(value.get("sha256") or ""))
                or not isinstance(value.get("byteCount"), int)
                or value.get("captureRef") != f"raw:/codex/{source_id}/{reference}"
            ):
                problems.append(f"{LEGACY_MANIFEST_NAME}:{number}: unsupported or inconsistent record")
                continue
            if reference in seen:
                problems.append(f"{LEGACY_MANIFEST_NAME}:{number}: duplicate sourceRef {reference}")
                continue
            seen.add(reference)
            records.append((number, value))
    return records


def _tree_files(root: Path) -> set[str]:
    found: set[str] = set()
    for tree in LEGACY_TREES:
        base = root / tree
        if not os.path.isdir(os_path(base)):
            continue
        for directory, _dirs, files in os.walk(os_path(base)):
            for name in files:
                absolute = os.path.join(directory, name)
                found.add(os.path.relpath(absolute, os_path(root)).replace("\\", "/"))
    return found


def _base(target: TargetConfig, run_id: str, relative: str) -> dict[str, Any]:
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "recordType": "version",
        "recordId": str(uuid.uuid4()),
        "runId": run_id,
        "profile": target.profile.name,
        "software": target.profile.software,
        "sourceId": target.source_id,
        "deviceId": target.device_id,
        "environment": target.environment,
        "hostName": None,
        "sourceRoot": str(target.source_root),
        "sourcePath": str(target.source_root.joinpath(*relative.split("/"))),
        "sourcePathBasis": "reconstructed",
        "relativePath": relative,
    }


def _existing_adopt_manifest(archive: ArchiveRoot) -> Path | None:
    """A finished adopt manifest left by an adoption interrupted before its marker was written."""

    names = sorted(os.listdir(os_path(archive.manifests_dir)))
    if len(names) != 1 or not names[0].endswith(".jsonl"):
        raise ArchiveError(f"unexpected files in {archive.manifests_dir}; inspect them before adopting")
    path = archive.manifests_dir / names[0]
    records, truncated = read_manifest_lines(path)
    if (
        truncated
        or not records
        or records[-1].get("recordType") != "run"
        or records[-1].get("command") != "legacy adopt"
    ):
        raise ArchiveError(f"{path} is not a finished legacy adoption manifest; inspect it before adopting")
    return path


def adopt_legacy(target: TargetConfig, *, dry_run: bool, run_id: str) -> dict[str, Any]:
    started = now_local()
    archive = ArchiveRoot(target.archive_root)
    result: dict[str, Any] = {
        "targetId": target.target_id,
        "archiveRoot": str(target.archive_root),
        "sourceRoot": str(target.source_root),
        "status": "failed",
    }
    if target.profile.name != "codex":
        raise ArchiveError("legacy adopt supports only targets with profile codex")
    state = archive.state()
    if state == "owned":
        marker = archive.read_marker()
        assert marker is not None
        archive.check_identity(target, marker)
        result.update(status="already-adopted", legacyStore=marker.get("legacyStore"))
        return result
    if state != "legacy":
        raise ArchiveError(f"no tkn-codex-chat-note-pipeline Raw store found at {archive.root} (state: {state})")
    root = archive.root
    legacy_marker = _read_json(root / LEGACY_MARKER_NAME)
    if (
        not isinstance(legacy_marker, dict)
        or legacy_marker.get("applicationId") != LEGACY_APPLICATION_ID
        or legacy_marker.get("rootKind") != "raw"
        or legacy_marker.get("sourceProvider") not in (None, "codex")
    ):
        raise ArchiveError(f"legacy marker is not a tkn-codex-chat-note-pipeline Raw root: {root / LEGACY_MARKER_NAME}")
    if legacy_marker.get("sourceId") not in (None, target.source_id):
        raise ArchiveError(
            f"legacy store belongs to sourceId {legacy_marker.get('sourceId')!r}, not {target.source_id!r}"
        )
    entries = set(archive.entries())
    resume_manifest = _existing_adopt_manifest(archive) if MANIFESTS_DIR in entries else None
    unexpected = sorted(entries - LEGACY_ALLOWED_ENTRIES - {MANIFESTS_DIR})
    problems: list[str] = [f"unexpected entry in legacy root: {name}" for name in unexpected]
    marker_sha, _ = sha256_file(root / LEGACY_MARKER_NAME)
    manifest_sha, _ = sha256_file(root / LEGACY_MANIFEST_NAME)
    legacy = _legacy_records(root, target.source_id, problems)
    LOGGER.info("%s: verifying %d legacy files", target.target_id, len(legacy))
    records: list[dict[str, Any]] = []
    for number, value in legacy:
        reference = str(value["sourceRef"])
        path = root.joinpath(*reference.split("/"))
        if not os.path.isfile(os_path(path)):
            problems.append(f"missing legacy file: {reference}")
            continue
        digest = _digest(path)
        if digest.sha256 != value["sha256"] or digest.size != value["byteCount"]:
            problems.append(f"legacy file does not match its manifest: {reference}")
            continue
        record = _base(target, run_id, reference)
        legacy_info: dict[str, Any] = {
            "applicationId": LEGACY_APPLICATION_ID,
            "manifest": LEGACY_MANIFEST_NAME,
            "line": number,
            "schemaVersion": value["schemaVersion"],
            "captureRef": value["captureRef"],
            "threadId": value.get("threadId"),
            "lastEventAt": value.get("lastEventAt"),
        }
        if value.get("metadataError"):
            legacy_info["metadataError"] = value["metadataError"]
        record.update(
            storedPath=reference,
            captureMethod="legacy-import",
            capturedAt=value.get("capturedAt"),
            byteCount=digest.size,
            sha256=digest.sha256,
            content=digest.finish(),
            sourceStat=None,
            previous=None,
            legacy=legacy_info,
            pipeline={"name": APPLICATION_ID, "version": __version__},
            result="imported",
        )
        records.append(record)
    listed = {str(value["sourceRef"]) for _number, value in legacy}
    for unlisted in sorted(_tree_files(root) - listed):
        problems.append(f"file not listed in the legacy manifest: {unlisted}")
    metadata: list[tuple[int, dict[str, Any]]] = []
    metadata_dir = root / "metadata"
    if os.path.isdir(os_path(metadata_dir)):
        for name in sorted(os.listdir(os_path(metadata_dir))):
            path = metadata_dir / name
            match = METADATA_NAME.fullmatch(name)
            if not match or not os.path.isfile(os_path(path)):
                problems.append(f"unexpected entry in legacy metadata: {name}")
                continue
            digest = _digest(path)
            if digest.sha256 != match.group(1):
                problems.append(f"legacy metadata snapshot does not match its name: {name}")
                continue
            modified = os.stat(os_path(path)).st_mtime_ns
            record = _base(target, run_id, APP_STATE_RELATIVE_PATH)
            record.update(
                storedPath=f"metadata/{name}",
                captureMethod="legacy-import",
                capturedAt=iso_from_ns(modified),
                capturedAtBasis="storedFileModifiedAt",
                byteCount=digest.size,
                sha256=digest.sha256,
                content=digest.finish(),
                sourceStat=None,
                previous=None,
                legacy={"applicationId": LEGACY_APPLICATION_ID, "kind": "appStateSnapshot", "storedName": name},
                pipeline={"name": APPLICATION_ID, "version": __version__},
                result="imported",
            )
            metadata.append((modified, record))
    metadata.sort(key=lambda item: item[0])
    result.update(
        legacyManifestRecords=len(legacy),
        importedVersions=len(records),
        metadataSnapshots=len(metadata),
        importedBytes=sum(int(item["byteCount"]) for item in records) + sum(int(r["byteCount"]) for _m, r in metadata),
        problems=problems,
    )
    if problems:
        result["status"] = "failed"
        result["error"] = f"{len(problems)} problem(s) found; nothing was written"
        return result
    legacy_store = {
        "applicationId": LEGACY_APPLICATION_ID,
        "markerSha256": marker_sha,
        "manifestSha256": manifest_sha,
        "manifestRecordCount": len(legacy),
        "metadataSnapshotCount": len(metadata),
        "adoptedAt": iso_seconds(started),
        "adoptRunId": run_id,
    }
    if dry_run:
        result.update(status="planned", legacyStore=legacy_store)
        return result
    with archive.lock():
        if (
            sha256_file(root / LEGACY_MANIFEST_NAME)[0] != manifest_sha
            or sha256_file(root / LEGACY_MARKER_NAME)[0] != marker_sha
        ):
            raise ArchiveError("legacy store changed during adoption; nothing was written")
        if resume_manifest is None:
            writer = ManifestWriter(archive, run_id, read_only=target.read_only)
            try:
                for record in [*records, *(record for _m, record in metadata)]:
                    writer.append(record)
                manifest_path = writer.finalize(
                    {
                        "schemaVersion": MANIFEST_SCHEMA_VERSION,
                        "recordType": "run",
                        "runId": run_id,
                        "targetId": target.target_id,
                        "command": "legacy adopt",
                        "status": "complete",
                        "startedAt": iso_seconds(started),
                        "finishedAt": iso_seconds(now_local()),
                        "pipeline": {"name": APPLICATION_ID, "version": __version__},
                        "counts": {"imported": len(records), "metadataSnapshots": len(metadata)},
                        "legacyStore": legacy_store,
                    }
                )
            finally:
                writer.close()
        else:
            manifest_path = resume_manifest
            legacy_store["adoptRunId"] = manifest_path.name[: -len(".jsonl")]
        archive.write_marker(archive.new_marker(target, iso_seconds(started), legacy_store), read_only=target.read_only)
    result.update(
        status="adopted", manifestPath=str(manifest_path), markerPath=str(archive.marker_path), legacyStore=legacy_store
    )
    return result
