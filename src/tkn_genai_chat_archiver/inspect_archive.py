"""Read-only commands: status and verify."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .archive import ArchiveRoot, sha256_file
from .config import AppConfig, TargetConfig
from .fsutil import ArchiveError, os_path

LOGGER = logging.getLogger("tkn_genai_chat_archiver")


def target_status(config: AppConfig, target: TargetConfig) -> dict[str, Any]:
    archive = ArchiveRoot(target.archive_root)
    result: dict[str, Any] = {
        "targetId": target.target_id,
        "profile": target.profile.name,
        "enabled": target.enabled,
        "sources": [
            {
                **({"name": root.name} if root.name else {}),
                "path": str(root.path),
                "available": os.path.isdir(os_path(root.path)),
            }
            for root in target.source_roots
        ],
        "archiveRoot": str(target.archive_root),
        "status": "success",
    }
    try:
        state = archive.state()
        result["archiveState"] = state
        if state != "owned":
            return result
        marker = archive.read_marker()
        assert marker is not None
        archive.check_identity(target, marker)
        result["adoptedLegacyStore"] = bool(marker.get("legacyStore"))
        try:
            archive.check_legacy_guard(marker)
            result["legacyGuard"] = "ok" if marker.get("legacyStore") else "not-applicable"
        except ArchiveError as exc:
            result["legacyGuard"] = "changed"
            result["status"] = "failed"
            result["error"] = str(exc)
        index = archive.load_index()
        latest_runs = sorted(index.run_ids)
        result.update(
            versionCount=index.version_count,
            pathCount=len(index.paths),
            sourceMissingPathCount=sum(1 for item in index.paths.values() if item.missing),
            storedBytes=index.stored_bytes,
            manifestCount=len(index.manifest_files),
            latestRunId=latest_runs[-1] if latest_runs else None,
            incompleteRuns=[str(path) for path in index.partial_files],
            invalidManifests=index.invalid_files,
        )
        cache = config.target_state_root(target.target_id) / "stat-cache.json"
        if os.path.isfile(os_path(cache)):
            try:
                with open(os_path(cache), encoding="utf-8") as handle:
                    result["lastCaptureAt"] = json.load(handle).get("updatedAt")
            except (OSError, ValueError):
                result["lastCaptureAt"] = None
        if index.partial_files or index.invalid_files:
            result["status"] = "failed" if index.invalid_files else result["status"]
    except ArchiveError as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
    return result


def verify_target(target: TargetConfig) -> dict[str, Any]:
    archive = ArchiveRoot(target.archive_root)
    result: dict[str, Any] = {"targetId": target.target_id, "archiveRoot": str(target.archive_root), "status": "failed"}
    try:
        if archive.state() != "owned":
            raise ArchiveError(f"not an archive of this CLI: {archive.root}")
        marker = archive.read_marker()
        assert marker is not None
        archive.check_identity(target, marker)
        problems: list[dict[str, str]] = []
        try:
            archive.check_legacy_guard(marker)
        except ArchiveError as exc:
            problems.append({"storedPath": "", "problem": str(exc)})
        index = archive.load_index()
        checked = 0
        checked_bytes = 0
        for history in index.paths.values():
            for version in history.versions:
                path = archive.root.joinpath(*version.stored_path.split("/"))
                checked += 1
                if not os.path.isfile(os_path(path)):
                    problems.append({"storedPath": version.stored_path, "problem": "missing"})
                    continue
                digest, size = sha256_file(path)
                checked_bytes += size
                if digest != version.sha256 or size != version.byte_count:
                    problems.append({"storedPath": version.stored_path, "problem": "sha256 or size mismatch"})
                if checked % 500 == 0:
                    LOGGER.info("%s: %d versions verified", target.target_id, checked)
        for item in index.invalid_files:
            problems.append({"storedPath": item["path"], "problem": item["reason"]})
        result.update(
            status="failed" if problems else "success",
            checkedVersions=checked,
            checkedBytes=checked_bytes,
            incompleteRuns=[str(path) for path in index.partial_files],
            problems=problems,
        )
    except ArchiveError as exc:
        result["error"] = str(exc)
    return result
