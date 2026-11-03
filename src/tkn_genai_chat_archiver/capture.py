"""Scan a source folder and store new `(relativePath, sha256)` versions without touching the source."""

from __future__ import annotations

import errno
import json
import logging
import os
import platform
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import APPLICATION_ID, __version__
from .archive import (
    MANIFEST_SCHEMA_VERSION,
    ArchiveIndex,
    ArchiveRoot,
    ManifestWriter,
    VersionRef,
    read_manifest_lines,
    sha256_file,
)
from .config import AppConfig, TargetConfig
from .content import SQLITE_MAGIC, StreamDigest
from .fsutil import (
    CHUNK_SIZE,
    IS_WINDOWS,
    ArchiveError,
    is_link,
    iso_from_ns,
    iso_seconds,
    json_bytes,
    json_line,
    now_local,
    os_path,
    plain_path,
    rename_no_replace,
    replace_file,
    set_read_only,
    temporary_sibling,
    write_new_file,
)
from .patterns import PatternSet

LOGGER = logging.getLogger("tkn_genai_chat_archiver")
SQLITE_COMPANION_SUFFIXES = ("-wal", "-shm", "-journal")
STAT_CACHE_SCHEMA_VERSION = 1
SHARING_VIOLATION_WINERRORS = {32, 33}


@dataclass(frozen=True)
class CaptureOptions:
    dry_run: bool
    rehash: bool
    settle_seconds: int | None
    full_output: bool


@dataclass
class ScannedFile:
    relative_path: str
    path: str
    byte_count: int
    modified_ns: int
    root_name: str = ""
    root_path: str = ""


@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    unreadable_dirs: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    excluded: int = 0
    unavailable_roots: list[dict[str, str]] = field(default_factory=list)
    # Companion files present at the source but not selected this time (still present, so not missing).
    dropped_companions: set[str] = field(default_factory=set)


def scan_target(target: TargetConfig, patterns: PatternSet) -> ScanResult:
    """Scan every source folder of a target; relative paths of named roots start with the root name."""

    result = ScanResult()
    for root in target.source_roots:
        if not os.path.isdir(os_path(root.path)):
            result.unavailable_roots.append({"name": root.name, "path": str(root.path)})
            result.unreadable_dirs.append(root.name)
            continue
        scan_source(root.path, patterns, prefix=root.name, result=result)
    if target.companion_include:
        companions = PatternSet(target.companion_include, (), case_insensitive=IS_WINDOWS)
        folders: set[str] = set()
        for item in result.files:
            if companions.selects(item.relative_path):
                continue
            parts = item.relative_path.split("/")
            folders.update("/".join(parts[:index]) for index in range(1, len(parts)))
        kept = []
        for item in result.files:
            parent = item.relative_path.rsplit("/", 1)[0] if "/" in item.relative_path else ""
            if companions.selects(item.relative_path) and parent not in folders:
                result.dropped_companions.add(item.relative_path)
                continue
            kept.append(item)
        result.files = kept
    result.files.sort(key=lambda item: item.relative_path)
    return result


def scan_source(root: Path, patterns: PatternSet, *, prefix: str = "", result: ScanResult | None = None) -> ScanResult:
    result = result if result is not None else ScanResult()
    stack: list[tuple[str, str]] = [(prefix, os_path(root))]
    while stack:
        relative_dir, absolute_dir = stack.pop()
        try:
            with os.scandir(absolute_dir) as iterator:
                entries = list(iterator)
        except OSError as exc:
            result.errors.append({"relativePath": relative_dir or ".", "error": str(exc)})
            result.unreadable_dirs.append(relative_dir)
            continue
        for entry in entries:
            relative = f"{relative_dir}/{entry.name}" if relative_dir else entry.name
            try:
                junction = bool(getattr(entry, "is_junction", lambda: False)())
                if entry.is_symlink() or junction or is_link(entry.path):
                    if patterns.selects(relative) or patterns.may_contain(relative):
                        result.links.append(relative)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if patterns.may_contain(relative):
                        stack.append((relative, entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False) or not patterns.included(relative):
                    continue
                if patterns.excluded(relative):
                    result.excluded += 1
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                result.errors.append({"relativePath": relative, "error": str(exc)})
                continue
            result.files.append(ScannedFile(relative, entry.path, info.st_size, info.st_mtime_ns, prefix, str(root)))
    result.files.sort(key=lambda item: item.relative_path)
    return result


def _sqlite_uri(path: str, query: str) -> str:
    plain = plain_path(path)
    if plain.startswith("\\\\"):
        return "file:" + quote("//" + plain.replace("\\", "/"), safe="/:") + "?" + query
    return Path(plain).as_uri() + "?" + query


def _read_header(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(len(SQLITE_MAGIC))


def _sqlite_info(header: bytes, staged: Path) -> dict[str, Any]:
    page_size = int.from_bytes(header[16:18], "big") if len(header) >= 18 else 0
    info: dict[str, Any] = {
        "libraryVersion": sqlite3.sqlite_version,
        "pageSize": 65536 if page_size == 1 else page_size,
        "pageCount": int.from_bytes(header[28:32], "big") if len(header) >= 32 else None,
        "journalMode": "wal" if len(header) > 18 and header[18] == 2 else "rollback",
    }
    try:
        connection = sqlite3.connect(_sqlite_uri(str(staged), "mode=ro&immutable=1"), uri=True)
        try:
            row = connection.execute("pragma quick_check").fetchone()
            info["quickCheck"] = str(row[0]) if row else None
        finally:
            connection.close()
    except sqlite3.Error as exc:
        info["quickCheck"] = f"error: {exc}"
    return info


def _file_signature(path: str) -> dict[str, int]:
    info = os.stat(path)
    return {"byteCount": info.st_size, "modifiedNs": info.st_mtime_ns}


@dataclass
class TargetRun:
    target: TargetConfig
    config: AppConfig
    options: CaptureOptions
    run_id: str
    started_at: str
    archive: ArchiveRoot
    index: ArchiveIndex
    patterns: PatternSet
    writer: ManifestWriter | None
    stat_cache: dict[str, dict[str, Any]]
    content_hashes: set[str]
    host_name: str
    counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("scanned", "stored", "planned", "unchanged", "deferred", "excluded", "links", "failed", "sourceMissing"),
            0,
        )
    )
    stored_bytes: int = 0
    planned_bytes: int = 0
    content_duplicates: int = 0
    deferred: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    new_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    def settle_exempt(self, relative: str) -> bool:
        if not self.target.settle_exempt:
            return False
        return PatternSet(self.target.settle_exempt, (), case_insensitive=IS_WINDOWS).selects(relative)

    @property
    def settle_seconds(self) -> int:
        if self.options.settle_seconds is not None:
            return self.options.settle_seconds
        return self.target.settle_seconds

    @property
    def snapshot_root(self) -> Path:
        return self.archive.snapshots_dir / self.run_id

    @property
    def staging_dir(self) -> Path:
        return self.config.target_state_root(self.target.target_id) / "staging"

    def defer(self, item: ScannedFile, reason: str, detail: str | None = None) -> None:
        self.counts["deferred"] += 1
        entry: dict[str, Any] = {"relativePath": item.relative_path, "reason": reason}
        if detail:
            entry["detail"] = detail
        self.deferred.append(entry)

    def fail(self, relative: str, error: str, source_path: str | None = None) -> None:
        self.counts["failed"] += 1
        entry = {"relativePath": relative, "error": error}
        if source_path:
            entry["sourcePath"] = plain_path(source_path)
        self.failed.append(entry)
        LOGGER.error("%s: %s: %s", self.target.target_id, relative, error)

    def remember(self, item: ScannedFile, sha256: str, extra: dict[str, int] | None = None) -> None:
        entry: dict[str, Any] = {"byteCount": item.byte_count, "modifiedNs": item.modified_ns, "sha256": sha256}
        if extra:
            entry.update(extra)
        self.new_cache[item.relative_path] = entry

    def cached_sha(self, item: ScannedFile, extra: dict[str, int] | None = None) -> str | None:
        if self.options.rehash:
            return None
        entry = self.stat_cache.get(item.relative_path)
        if not entry or entry.get("byteCount") != item.byte_count or entry.get("modifiedNs") != item.modified_ns:
            return None
        for key, value in (extra or {}).items():
            if entry.get(key) != value:
                return None
        sha = str(entry.get("sha256") or "")
        return sha if (item.relative_path, sha) in self.index.pairs else None

    def base_record(self, item: ScannedFile) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schemaVersion": MANIFEST_SCHEMA_VERSION,
            "recordType": "version",
            "recordId": str(uuid.uuid4()),
            "runId": self.run_id,
            "profile": self.target.profile.name,
            "software": self.target.profile.software,
            "sourceId": self.target.source_id,
            "deviceId": self.target.device_id,
            "environment": self.target.environment,
            "hostName": self.host_name,
            "sourceRoot": item.root_path or str(self.target.source_root),
        }
        if item.root_name:
            record["sourceRootName"] = item.root_name
        record.update(sourcePath=plain_path(item.path), relativePath=item.relative_path)
        return record

    def previous_of(self, relative: str) -> VersionRef | None:
        history = self.index.paths.get(relative)
        return history.latest if history else None


def _previous_field(previous: VersionRef | None, prefix_sha256: str | None) -> dict[str, Any] | None:
    if previous is None:
        return None
    relation = "appended" if prefix_sha256 is not None and prefix_sha256 == previous.sha256 else "rewritten"
    return {
        "recordId": previous.record_id,
        "sha256": previous.sha256,
        "byteCount": previous.byte_count,
        "relation": relation,
    }


def _place(run: TargetRun, staged: Path, relative: str) -> Path:
    destination = run.snapshot_root.joinpath(*relative.split("/"))
    os.makedirs(os_path(destination.parent), exist_ok=True)
    if os.path.lexists(os_path(destination)):
        raise ArchiveError(f"refusing to replace an existing archive file: {destination}")
    try:
        rename_no_replace(os_path(staged), os_path(destination))
    except FileExistsError as exc:
        raise ArchiveError(f"refusing to replace an existing archive file: {destination}") from exc
    except OSError as exc:
        if getattr(exc, "winerror", None) != 17 and exc.errno != errno.EXDEV:
            raise
        # Different volume: copy beside the destination, then move into place without replacing.
        temporary = temporary_sibling(destination)
        try:
            shutil.copyfile(os_path(staged), os_path(temporary))
            rename_no_replace(os_path(temporary), os_path(destination))
        finally:
            if os.path.exists(os_path(temporary)):
                os.unlink(os_path(temporary))
        os.unlink(os_path(staged))
    return destination


def _finish_version(
    run: TargetRun,
    item: ScannedFile,
    staged: Path,
    digest: StreamDigest,
    content: dict[str, Any],
    record_extra: dict[str, Any],
) -> None:
    destination = _place(run, staged, item.relative_path)
    stored_sha, stored_size = sha256_file(destination)
    if stored_sha != digest.sha256 or stored_size != digest.size:
        # Keep the bad copy for inspection, under a name no reader treats as a version.
        failed_path = destination.with_name(destination.name + ".verify-failed")
        rename_no_replace(os_path(destination), os_path(failed_path))
        raise ArchiveError(f"stored copy does not match after writing: {failed_path}")
    if run.target.read_only:
        set_read_only(destination)
    previous = run.previous_of(item.relative_path)
    stored_path = f"snapshots/{run.run_id}/{item.relative_path}"
    record = run.base_record(item)
    record.update(
        storedPath=stored_path,
        capturedAt=iso_seconds(now_local()),
        byteCount=digest.size,
        sha256=digest.sha256,
        content=content,
        sourceStat={"byteCount": item.byte_count, "modifiedAt": iso_from_ns(item.modified_ns)},
        previous=_previous_field(previous, digest.prefix_sha256),
        pipeline={"name": APPLICATION_ID, "version": __version__},
        result="stored",
        **record_extra,
    )
    assert run.writer is not None
    run.writer.append(record)
    run.index.add(record)
    run.counts["stored"] += 1
    run.stored_bytes += digest.size
    duplicate = bool(content.get("sha256")) and content.get("sha256") in run.content_hashes
    run.content_duplicates += int(duplicate)
    if content.get("sha256"):
        run.content_hashes.add(str(content["sha256"]))
    run.items.append(
        {
            "relativePath": item.relative_path,
            "action": "stored",
            "captureMethod": record["captureMethod"],
            "byteCount": digest.size,
            "contentEncoding": content.get("encoding"),
            "contentAlreadyArchived": duplicate,
            "relation": record["previous"]["relation"] if record["previous"] else None,
        }
    )


def _plan(run: TargetRun, item: ScannedFile, method: str, digest: StreamDigest | None, content: dict[str, Any]) -> None:
    run.counts["planned"] += 1
    size = digest.size if digest else item.byte_count
    run.planned_bytes += size
    duplicate = bool(content.get("sha256")) and content.get("sha256") in run.content_hashes
    run.content_duplicates += int(duplicate)
    previous = _previous_field(run.previous_of(item.relative_path), digest.prefix_sha256 if digest else None)
    run.items.append(
        {
            "relativePath": item.relative_path,
            "action": "planned",
            "captureMethod": method,
            "byteCount": size,
            "contentEncoding": content.get("encoding"),
            "contentAlreadyArchived": duplicate,
            "relation": previous["relation"] if previous else None,
        }
    )


def _hash_source(path: str, prefix_length: int | None) -> StreamDigest:
    digest = StreamDigest(prefix_length=prefix_length)
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest


def _capture_copy(run: TargetRun, item: ScannedFile) -> None:
    before = os.stat(item.path)
    previous = run.previous_of(item.relative_path)
    prefix = previous.byte_count if previous and before.st_size >= previous.byte_count else None
    digest = _hash_source(item.path, prefix)
    content = digest.finish()
    if (item.relative_path, digest.sha256) in run.index.pairs:
        run.counts["unchanged"] += 1
        run.remember(item, digest.sha256)
        return
    if run.options.dry_run:
        _plan(run, item, "copy", digest, content)
        return
    os.makedirs(os_path(run.staging_dir), exist_ok=True)
    staged = run.staging_dir / f"{uuid.uuid4().hex}.part"
    copy_digest = StreamDigest()
    try:
        with open(item.path, "rb") as source, open(os_path(staged), "xb") as target:
            while chunk := source.read(CHUNK_SIZE):
                copy_digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = os.stat(item.path)
        if (
            copy_digest.sha256 != digest.sha256
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            run.defer(item, "changed-during-copy")
            return
        _finish_version(run, item, staged, digest, content, {"captureMethod": "copy"})
        run.remember(item, digest.sha256)
    finally:
        if os.path.exists(os_path(staged)):
            os.unlink(os_path(staged))


def _capture_sqlite(run: TargetRun, item: ScannedFile, header: bytes) -> None:
    wal_path = item.path + "-wal"
    wal = _file_signature(wal_path) if os.path.exists(wal_path) else {"byteCount": -1, "modifiedNs": -1}
    extra = {"walByteCount": wal["byteCount"], "walModifiedNs": wal["modifiedNs"]}
    cached = run.cached_sha(item, extra)
    if cached:
        run.counts["unchanged"] += 1
        run.remember(item, cached, extra)
        return
    if run.options.dry_run:
        # A dry run does not create the backup, so a change in file stats is reported as planned.
        _plan(run, item, "sqlite-backup", None, {"encoding": "identity"})
        return
    os.makedirs(os_path(run.staging_dir), exist_ok=True)
    staged = run.staging_dir / f"{uuid.uuid4().hex}.sqlite"
    try:
        try:
            source = sqlite3.connect(_sqlite_uri(item.path, "mode=ro"), uri=True, timeout=10)
            try:
                destination = sqlite3.connect(os_path(staged))
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
        except sqlite3.OperationalError as exc:
            message = str(exc)
            if "locked" in message or "busy" in message:
                run.defer(item, "sqlite-busy", message)
            else:
                run.fail(item.relative_path, f"sqlite backup failed: {message}", item.path)
            return
        digest = StreamDigest()
        with open(os_path(staged), "rb") as handle:
            staged_header = handle.read(100)
            digest.update(staged_header)
            while chunk := handle.read(CHUNK_SIZE):
                digest.update(chunk)
        content = digest.finish()
        if (item.relative_path, digest.sha256) in run.index.pairs:
            run.counts["unchanged"] += 1
            run.remember(item, digest.sha256, extra)
            return
        info = _sqlite_info(staged_header, staged)
        source_files = [(item.path, item.byte_count, item.modified_ns)]
        if wal["byteCount"] >= 0:
            source_files.append((wal_path, wal["byteCount"], wal["modifiedNs"]))
        info["sourceFiles"] = [
            {"name": Path(plain_path(path)).name, "byteCount": size, "modifiedAt": iso_from_ns(modified)}
            for path, size, modified in source_files
        ]
        _finish_version(run, item, staged, digest, content, {"captureMethod": "sqlite-backup", "sqlite": info})
        run.remember(item, digest.sha256, extra)
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            leftover = os_path(str(staged) + suffix)
            if os.path.exists(leftover):
                os.unlink(leftover)


def _is_sqlite_companion(item: ScannedFile) -> bool:
    for suffix in SQLITE_COMPANION_SUFFIXES:
        if item.path.endswith(suffix):
            base = item.path[: -len(suffix)]
            try:
                return os.path.isfile(base) and _read_header(base) == SQLITE_MAGIC
            except OSError:
                return False
    return False


def _process(run: TargetRun, item: ScannedFile) -> None:
    run.counts["scanned"] += 1
    if _is_sqlite_companion(item):
        run.counts["excluded"] += 1
        return
    try:
        header = _read_header(item.path)
        if header == SQLITE_MAGIC:
            _capture_sqlite(run, item, header)
            return
        settle_ns = run.settle_seconds * 1_000_000_000
        if settle_ns and not run.settle_exempt(item.relative_path) and time.time_ns() - item.modified_ns < settle_ns:
            run.defer(item, "recently-modified")
            return
        cached = run.cached_sha(item)
        if cached:
            run.counts["unchanged"] += 1
            run.remember(item, cached)
            return
        _capture_copy(run, item)
    except ArchiveError as exc:
        run.fail(item.relative_path, str(exc), item.path)
    except OSError as exc:
        if getattr(exc, "winerror", None) in SHARING_VIOLATION_WINERRORS:
            run.defer(item, "in-use", str(exc))
        elif isinstance(exc, FileNotFoundError):
            run.defer(item, "disappeared", str(exc))
        else:
            run.fail(item.relative_path, f"{type(exc).__name__}: {exc}", item.path)


def _record_missing(run: TargetRun, scan: ScanResult) -> None:
    seen = {item.relative_path for item in scan.files} | scan.dropped_companions
    for relative, history in sorted(run.index.paths.items()):
        if history.missing or history.latest is None or relative in seen or not run.patterns.selects(relative):
            continue
        # A folder that could not be listed says nothing about its files.
        if any(not prefix or relative.startswith(prefix + "/") for prefix in scan.unreadable_dirs):
            continue
        run.counts["sourceMissing"] += 1
        if run.options.dry_run:
            continue
        record = {
            "schemaVersion": MANIFEST_SCHEMA_VERSION,
            "recordType": "sourceMissing",
            "runId": run.run_id,
            "relativePath": relative,
            "lastRecordId": history.latest.record_id,
            "observedAt": iso_seconds(now_local()),
        }
        assert run.writer is not None
        run.writer.append(record)
        run.index.add(record)


def _load_stat_cache(path: Path, archive_root: Path) -> dict[str, dict[str, Any]]:
    try:
        with open(os_path(path), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != STAT_CACHE_SCHEMA_VERSION
        or value.get("archiveRoot") != str(archive_root)
        or not isinstance(value.get("entries"), dict)
    ):
        return {}
    return {str(key): item for key, item in value["entries"].items() if isinstance(item, dict)}


def recover_partials(archive: ArchiveRoot, index: ArchiveIndex, read_only: bool) -> list[dict[str, Any]]:
    """Finalize interrupted manifests whose stored versions all verify; leave others for inspection."""

    results: list[dict[str, Any]] = []
    for partial in list(index.partial_files):
        run_id = partial.name[: -len(".jsonl.partial")]
        try:
            records, truncated = read_manifest_lines(partial)
            problems = []
            for record in records:
                if record.get("recordType") != "version":
                    continue
                stored = archive.root.joinpath(*str(record["storedPath"]).split("/"))
                if not os.path.isfile(os_path(stored)):
                    problems.append(f"missing {record['storedPath']}")
                    continue
                digest, size = sha256_file(stored)
                if digest != record["sha256"] or size != record["byteCount"]:
                    problems.append(f"mismatch {record['storedPath']}")
            if problems:
                results.append({"runId": run_id, "status": "left-incomplete", "problems": problems[:20]})
                continue
            run_record = {
                "schemaVersion": MANIFEST_SCHEMA_VERSION,
                "recordType": "run",
                "runId": run_id,
                "status": "recovered",
                "recoveredAt": iso_seconds(now_local()),
                "droppedTruncatedLine": truncated,
                "pipeline": {"name": APPLICATION_ID, "version": __version__},
                "counts": {"stored": sum(1 for item in records if item.get("recordType") == "version")},
            }
            final = partial.with_name(f"{run_id}.jsonl")
            write_new_file(final, b"".join(json_line(item) for item in [*records, run_record]))
            if read_only:
                set_read_only(final)
            os.unlink(os_path(partial))
            for record in records:
                index.add(record)
            index.partial_files.remove(partial)
            index.run_ids.add(run_id)
            results.append({"runId": run_id, "status": "recovered", "records": len(records)})
        except (OSError, ArchiveError) as exc:
            results.append({"runId": run_id, "status": "left-incomplete", "problems": [str(exc)]})
    return results


def prepare_root(archive: ArchiveRoot, target: TargetConfig, *, dry_run: bool, created_at: str) -> str:
    state = archive.state()
    if state == "legacy":
        raise ArchiveError(
            f"archive root was created by tkn-codex-chat-note-pipeline; run `legacy adopt --target "
            f"{target.target_id}` first: {archive.root}"
        )
    if state == "foreign":
        raise ArchiveError(f"refusing to write to a non-empty folder without an archive marker: {archive.root}")
    if state == "owned":
        marker = archive.read_marker()
        assert marker is not None
        archive.check_identity(target, marker)
        archive.check_legacy_guard(marker)
        return "owned"
    if dry_run:
        return "would-initialize"
    os.makedirs(os_path(archive.root), exist_ok=True)
    archive.write_marker(archive.new_marker(target, created_at, None), read_only=target.read_only)
    return "initialized"


def capture_target(config: AppConfig, target: TargetConfig, options: CaptureOptions, run_id: str) -> dict[str, Any]:
    started = now_local()
    result: dict[str, Any] = {
        "targetId": target.target_id,
        "profile": target.profile.name,
        "software": target.profile.software,
        **(
            {"sourceRoots": {root.name: str(root.path) for root in target.source_roots}}
            if target.multi_root
            else {"sourceRoot": str(target.source_root)}
        ),
        "archiveRoot": str(target.archive_root),
        "status": "failed",
    }
    available = [root for root in target.source_roots if os.path.isdir(os_path(root.path))]
    if not available:
        result["status"] = "skipped" if target.optional else "failed"
        result["reason"] = "source-unavailable"
        folders = ", ".join(str(root.path) for root in target.source_roots)
        result["error"] = f"source folder is not available: {folders}"
        log = LOGGER.warning if target.optional else LOGGER.error
        log("%s: %s", target.target_id, result["error"])
        return result
    archive = ArchiveRoot(target.archive_root)
    try:
        archive_state = prepare_root(archive, target, dry_run=options.dry_run, created_at=iso_seconds(started))
        result["archiveState"] = archive_state
        if options.dry_run:
            return _run_target(config, target, options, run_id, started, archive, result)
        with archive.lock():
            # Check again under the lock.
            prepare_root(archive, target, dry_run=False, created_at=iso_seconds(started))
            return _run_target(config, target, options, run_id, started, archive, result)
    except ArchiveError as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        LOGGER.error("%s: %s", target.target_id, exc)
        return result


def _run_target(
    config: AppConfig,
    target: TargetConfig,
    options: CaptureOptions,
    run_id: str,
    started: Any,
    archive: ArchiveRoot,
    result: dict[str, Any],
) -> dict[str, Any]:
    index = archive.load_index()
    if not options.dry_run and index.partial_files:
        result["recoveredRuns"] = recover_partials(archive, index, target.read_only)
    elif index.partial_files:
        result["incompleteRuns"] = [str(path) for path in index.partial_files]
    if index.invalid_files:
        result["invalidManifests"] = index.invalid_files
    unique = archive.unique_run_id(run_id, index)
    cache_path = config.target_state_root(target.target_id) / "stat-cache.json"
    content_hashes = {
        str(version.record.get("content", {}).get("sha256"))
        for history in index.paths.values()
        for version in history.versions
        if isinstance(version.record.get("content"), dict) and version.record["content"].get("sha256")
    }
    run = TargetRun(
        target=target,
        config=config,
        options=options,
        run_id=unique,
        started_at=iso_seconds(started),
        archive=archive,
        index=index,
        patterns=target.patterns,
        writer=None if options.dry_run else ManifestWriter(archive, unique, read_only=target.read_only),
        stat_cache=_load_stat_cache(cache_path, target.archive_root),
        content_hashes=content_hashes,
        host_name=platform.node(),
    )
    if not options.dry_run and os.path.isdir(os_path(run.staging_dir)):
        shutil.rmtree(os_path(run.staging_dir), ignore_errors=True)
    LOGGER.info("%s: scanning %s", target.target_id, ", ".join(str(root.path) for root in target.source_roots))
    scan = scan_target(target, run.patterns)
    for root in scan.unavailable_roots:
        # Another folder of this target is available; a missing one (e.g. an unused CLI) is not a failure.
        message = f"source folder {root['name']} is not available: {root['path']}"
        run.warnings.append(message)
        LOGGER.warning("%s: %s", target.target_id, message)
    run.counts["excluded"] += scan.excluded + len(scan.dropped_companions)
    run.counts["links"] = len(scan.links)
    for error in scan.errors:
        run.fail(error["relativePath"], f"cannot read: {error['error']}")
    for number, item in enumerate(scan.files, 1):
        _process(run, item)
        if number % 200 == 0:
            LOGGER.info("%s: %d/%d files checked", target.target_id, number, len(scan.files))
    _record_missing(run, scan)
    status = "failed" if run.failed else "success"
    result.update(
        status=status,
        runId=unique,
        counts=run.counts,
        storedBytes=run.stored_bytes,
        contentAlreadyArchivedCount=run.content_duplicates,
        deferred=run.deferred,
        failed=run.failed,
        warnings=run.warnings,
        items=run.items,
    )
    if options.dry_run:
        result["plannedBytes"] = run.planned_bytes
        result["status"] = "failed" if run.failed else "planned"
        return result
    assert run.writer is not None
    if run.writer.started or run.failed:
        run_record = {
            "schemaVersion": MANIFEST_SCHEMA_VERSION,
            "recordType": "run",
            "runId": unique,
            "targetId": target.target_id,
            "status": "failed" if run.failed else "complete",
            "startedAt": run.started_at,
            "finishedAt": iso_seconds(now_local()),
            "pipeline": {"name": APPLICATION_ID, "version": __version__},
            "settings": {
                "settleSeconds": run.settle_seconds,
                "include": list(target.include),
                "exclude": list(target.exclude),
                "settleExempt": list(target.settle_exempt),
                "rehash": options.rehash,
            },
            "counts": run.counts,
            "deferred": run.deferred,
            "failed": run.failed,
        }
        manifest = run.writer.finalize(run_record)
        result["manifestPath"] = str(manifest)
        if run.counts["stored"]:
            result["snapshotPath"] = str(run.snapshot_root)
    else:
        run.writer.close()
    cache = {
        "schemaVersion": STAT_CACHE_SCHEMA_VERSION,
        "archiveRoot": str(target.archive_root),
        "updatedAt": iso_seconds(now_local()),
        "entries": run.new_cache,
    }
    replace_file(cache_path, json_bytes(cache, indent=None))
    LOGGER.info(
        "%s: stored=%d unchanged=%d deferred=%d failed=%d",
        target.target_id,
        run.counts["stored"],
        run.counts["unchanged"],
        run.counts["deferred"],
        run.counts["failed"],
    )
    return result
