from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest
import zstandard

from tkn_genai_chat_archiver.fsutil import IS_WINDOWS, os_path

from .conftest import Env, sha, write

ROLL = "sessions/2026/11/02/rollout-2026-11-02T08-00-00-0000.jsonl"


def stored(env: Env, record: dict[str, object]) -> Path:
    return env.archive.joinpath(*str(record["storedPath"]).split("/"))


def tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_capture_stores_exact_bytes_and_second_run_writes_nothing(env: Env) -> None:
    data = b'{"type":"session_meta"}\n{"type":"event_msg"}\n'
    write(env.source / ROLL, data)
    write(env.source / "session_index.jsonl", b'{"id":"x","thread_name":"t"}\n')
    write(env.source / "auth.json", b'{"token":"secret"}')
    write(env.source / "sessions" / "sync.ffs_db", b"other tool")
    env.configure()

    target = env.capture()

    assert target["counts"]["stored"] == 2
    assert target["counts"]["excluded"] == 1
    version = env.versions(ROLL)[0]
    assert stored(env, version).read_bytes() == data
    assert version["sha256"] == sha(data)
    assert version["content"] == {"encoding": "identity", "byteCount": len(data), "sha256": sha(data)}
    assert version["storedPath"] == f"snapshots/{target['runId']}/{ROLL}"
    assert version["deviceId"] == "Test PC"
    assert version["previous"] is None
    assert not env.versions("auth.json")
    assert (env.archive / ".tkn-genai-chat-archive.json").is_file()
    assert env.records()[-1]["recordType"] == "run"

    before = tree(env.archive)
    again = env.capture()
    assert again["counts"]["stored"] == 0
    assert again["counts"]["unchanged"] == 2
    assert "manifestPath" not in again
    assert tree(env.archive) == before


def test_changed_file_adds_version_and_keeps_the_old_one(env: Env) -> None:
    first = b'{"n":1}\n'
    write(env.source / ROLL, first)
    env.configure()
    env.capture()
    old = env.versions(ROLL)[0]

    write(env.source / ROLL, first + b'{"n":2}\n', mtime=1_700_000_100)
    env.capture()
    write(env.source / ROLL, b'{"n":9}\n', mtime=1_700_000_200)
    env.capture()

    versions = env.versions(ROLL)
    assert len(versions) == 3
    assert versions[1]["previous"]["relation"] == "appended"
    assert versions[1]["previous"]["recordId"] == old["recordId"]
    assert versions[2]["previous"]["relation"] == "rewritten"
    assert stored(env, old).read_bytes() == first
    assert len({item["storedPath"] for item in versions}) == 3


def test_reverted_content_is_not_stored_again(env: Env) -> None:
    write(env.source / ROLL, b"a\n")
    env.configure()
    env.capture()
    write(env.source / ROLL, b"b\n", mtime=1_700_000_100)
    env.capture()
    write(env.source / ROLL, b"a\n", mtime=1_700_000_200)
    target = env.capture()
    assert target["counts"]["stored"] == 0
    assert len(env.versions(ROLL)) == 2


def test_recently_modified_file_is_deferred(env: Env) -> None:
    write(env.source / ROLL, b"live\n", mtime=time.time())
    env.configure(settle_seconds=600)
    target = env.capture()
    assert target["counts"]["deferred"] == 1
    assert target["deferred"][0]["reason"] == "recently-modified"
    assert not env.manifests()
    target = env.capture("--settle-seconds", "0")
    assert target["counts"]["stored"] == 1


def test_dry_run_writes_nothing(env: Env) -> None:
    write(env.source / ROLL, b"x\n")
    env.configure()
    code, result = env.run("capture", "--dry-run")
    assert code == 0
    target = result["targets"][0]
    assert target["status"] == "planned"
    assert target["counts"]["planned"] == 1
    assert target["archiveState"] == "would-initialize"
    assert not (env.root / "archive").exists()
    assert not env.state.exists()


def test_zstd_file_is_stored_as_is_and_content_matches_plain(env: Env) -> None:
    data = b'{"line":1}\n' * 200
    write(env.source / ROLL, data)
    env.configure()
    env.capture()
    compressed = zstandard.ZstdCompressor().compress(data)
    (env.source / ROLL).unlink()
    write(env.source / (ROLL + ".zst"), compressed, mtime=1_700_000_100)

    target = env.capture()

    version = env.versions(ROLL + ".zst")[0]
    assert stored(env, version).read_bytes() == compressed
    assert version["content"]["encoding"] == "zstd"
    assert version["content"]["sha256"] == sha(data)
    assert target["contentAlreadyArchivedCount"] == 1
    missing = [item for item in env.records() if item["recordType"] == "sourceMissing"]
    assert [item["relativePath"] for item in missing] == [ROLL]
    env.capture()
    assert len([item for item in env.records() if item["recordType"] == "sourceMissing"]) == 1


def test_sqlite_wal_database_is_backed_up_consistently(env: Env) -> None:
    database = env.source / "antigravity-cli" / "conversations" / "c1.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute("pragma journal_mode=wal")
    connection.execute("pragma wal_autocheckpoint=0")
    connection.execute("create table steps (idx integer, data text)")
    connection.execute("insert into steps values (1, 'kept only in the WAL')")
    connection.commit()
    try:
        assert (database.parent / "c1.db-wal").stat().st_size > 0
        env.configure(profile="antigravity", include=["antigravity*/conversations/**"], exclude=[])
        target = env.capture()
        assert target["counts"]["stored"] == 1
        version = env.versions("antigravity-cli/conversations/c1.db")[0]
        assert version["captureMethod"] == "sqlite-backup"
        assert version["sqlite"]["quickCheck"] == "ok"
        assert [item["name"] for item in version["sqlite"]["sourceFiles"]] == ["c1.db", "c1.db-wal"]
        assert not env.versions("antigravity-cli/conversations/c1.db-wal")
        copy = sqlite3.connect(f"{stored(env, version).as_uri()}?mode=ro&immutable=1", uri=True)
        assert copy.execute("select data from steps").fetchall() == [("kept only in the WAL",)]
        copy.close()

        again = env.capture("--rehash")
        assert again["counts"]["stored"] == 0
        connection.execute("insert into steps values (2, 'more')")
        connection.commit()
        third = env.capture()
        assert third["counts"]["stored"] == 1
    finally:
        connection.close()


def test_stored_versions_and_manifests_are_read_only(env: Env) -> None:
    write(env.source / ROLL, b"x\n")
    env.configure()
    env.capture()
    version = env.versions(ROLL)[0]
    assert not os.stat(stored(env, version)).st_mode & stat.S_IWRITE
    assert not os.stat(env.manifests()[0]).st_mode & stat.S_IWRITE


def test_refuses_non_empty_folder_without_marker(env: Env) -> None:
    write(env.source / ROLL, b"x\n")
    write(env.archive / "something.txt", b"not ours")
    env.configure()
    code, result = env.run("capture")
    assert code == 1
    assert "non-empty" in result["targets"][0]["error"]
    assert not (env.archive / "manifests").exists()


def test_refuses_archive_of_another_source(env: Env) -> None:
    write(env.source / ROLL, b"x\n")
    env.configure()
    env.capture()
    env.configure(source_id="other-pc")
    code, result = env.run("capture")
    assert code == 1
    assert "sourceId" in result["targets"][0]["error"]


def test_interrupted_manifest_is_recovered(env: Env) -> None:
    write(env.source / ROLL, b"x\n")
    env.configure()
    env.capture()
    final = env.manifests()[0]
    partial = final.with_name("20000101T000000+0000.jsonl.partial")
    lines = final.read_text(encoding="utf-8").splitlines()
    partial.write_text("\n".join(lines[:-1]) + '\n{"truncated', encoding="utf-8")
    os.chmod(final, stat.S_IWRITE | stat.S_IREAD)
    final.unlink()

    target = env.capture()

    assert target["recoveredRuns"][0]["status"] == "recovered"
    assert not partial.exists()
    recovered = final.with_name("20000101T000000+0000.jsonl")
    assert recovered.read_text(encoding="utf-8").splitlines()[-1].count('"recovered"') == 1
    assert target["counts"]["stored"] == 0


def test_never_replaces_an_existing_snapshot_file(env: Env) -> None:
    from tkn_genai_chat_archiver.archive import ArchiveRoot
    from tkn_genai_chat_archiver.fsutil import ArchiveError

    write(env.source / ROLL, b"x\n")
    env.configure()
    env.capture()
    archive = ArchiveRoot(env.archive)
    marker = archive.read_marker()
    assert marker is not None
    occupied = env.archive / "snapshots" / "x" / "a.txt"
    write(occupied, b"keep")
    from tkn_genai_chat_archiver.fsutil import rename_no_replace

    other = env.root / "other.txt"
    other.write_bytes(b"new")
    with pytest.raises(FileExistsError):
        rename_no_replace(str(other), str(occupied))
    assert occupied.read_bytes() == b"keep"
    assert ArchiveError


@pytest.mark.skipif(not IS_WINDOWS, reason="MAX_PATH applies to Windows")
def test_long_paths_beyond_max_path(env: Env) -> None:
    deep = "projects/" + "/".join(["d" * 60] * 5) + "/session.jsonl"
    write(env.source.joinpath(*deep.split("/")), b"long\n")
    env.configure(profile="claude-code")
    target = env.capture()
    assert target["counts"]["stored"] == 1
    version = env.versions(deep)[0]
    path = os_path(stored(env, version))
    assert len(path) > 260
    with open(path, "rb") as handle:
        assert handle.read() == b"long\n"


def test_links_are_not_followed(env: Env) -> None:
    outside = env.root / "outside"
    write(outside / "secret.jsonl", b"outside\n")
    write(env.source / ROLL, b"x\n")
    try:
        os.symlink(outside, env.source / "sessions" / "linked", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not permitted here")
    env.configure()
    target = env.capture()
    assert target["counts"]["links"] == 1
    assert all("linked" not in item["relativePath"] for item in env.versions())


def test_verify_and_status(env: Env) -> None:
    write(env.source / ROLL, b"x\n")
    env.configure()
    env.capture()
    code, result = env.run("verify")
    assert code == 0
    assert result["targets"][0]["checkedVersions"] == 1
    code, result = env.run("status")
    assert code == 0
    assert result["targets"][0]["versionCount"] == 1
    version = env.versions(ROLL)[0]
    path = stored(env, version)
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    path.write_bytes(b"tampered")
    code, result = env.run("verify")
    assert code == 1
    assert result["targets"][0]["problems"][0]["problem"] == "sha256 or size mismatch"


def test_unavailable_optional_source_is_skipped(env: Env) -> None:
    env.configure(source_root=str(env.root / "missing"), optional=True)
    code, result = env.run("capture")
    assert code == 0
    assert result["targets"][0]["status"] == "skipped"
    env.configure(source_root=str(env.root / "missing"), optional=False)
    code, result = env.run("capture")
    assert code == 1


def test_settle_exempt_file_is_captured_while_recently_modified(env: Env) -> None:
    write(env.source / ".codex-global-state.json", b'{"a":1}', mtime=time.time())
    write(env.source / ROLL, b"live\n", mtime=time.time())
    env.configure(settle_seconds=600)
    target = env.capture()
    assert target["counts"]["stored"] == 1
    assert env.versions(".codex-global-state.json")
    assert [item["relativePath"] for item in target["deferred"]] == [ROLL]
