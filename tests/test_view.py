from __future__ import annotations

import gzip
import os

import pytest
import zstandard

from tkn_genai_chat_archiver.fsutil import os_path
from tkn_genai_chat_archiver.view import ArchiveError, ArchiveVersion, ArchiveView, is_archive_root

from .conftest import Env, sha, write
from .test_capture import ROLL, tree


def test_view_reads_versions_without_writing(env: Env) -> None:
    first = b'{"type":"session_meta"}\n'
    write(env.source / ROLL, first)
    env.configure()
    env.capture()
    second = first + b'{"type":"event_msg"}\n'
    write(env.source / ROLL, second, mtime=1_700_000_100)
    env.capture()
    before = tree(env.archive)

    view = ArchiveView(env.archive, profile="codex", source_id="pc-windows")

    assert is_archive_root(env.archive)
    assert view.profile == "codex" and view.source_id == "pc-windows" and view.manifest_count == 2
    assert view.relative_paths("sessions/") == [ROLL]
    versions = view.versions(ROLL)
    assert [item.content_sha256 for item in versions] == [sha(first), sha(second)]
    latest = view.latest(ROLL)
    assert latest is not None and latest.previous_relation == "appended"
    assert latest.previous_sha256 == sha(first)
    assert view.read_content(latest) == second
    assert view.ignored_manifests == []
    assert tree(env.archive) == before


def test_view_decodes_zstd_and_checks_hashes(env: Env) -> None:
    data = b'{"line":1}\n' * 50
    write(env.source / (ROLL + ".zst"), zstandard.ZstdCompressor().compress(data))
    env.configure()
    env.capture()
    view = ArchiveView(env.archive)
    version = view.latest(ROLL + ".zst")
    assert version is not None and version.content_encoding == "zstd"
    assert version.identity_sha256 == sha(data)
    assert view.read_content(version) == data

    stored = view.stored_file(version)
    os.chmod(os_path(stored), 0o644)
    with open(os_path(stored), "ab") as handle:
        handle.write(b"x")
    with pytest.raises(ArchiveError, match="does not match"):
        view.read_content(version)


def test_view_ignores_unfinished_manifests(env: Env) -> None:
    write(env.source / ROLL, b"{}\n")
    env.configure()
    env.capture()
    partial = env.archive / "manifests" / "20991231T000000+0900.jsonl.partial"
    partial.write_text('{"schemaVersion":"1.0.0","recordType":"version"}\n', encoding="utf-8")
    view = ArchiveView(env.archive)
    assert [item["reason"] for item in view.ignored_manifests] == ["partial"]
    assert len(view.versions(ROLL)) == 1


def test_view_refuses_other_identities_and_foreign_folders(env: Env, tmp_path: os.PathLike[str]) -> None:
    write(env.source / ROLL, b"{}\n")
    env.configure()
    env.capture()
    with pytest.raises(ArchiveError, match="profile"):
        ArchiveView(env.archive, profile="claude-code")
    with pytest.raises(ArchiveError, match="sourceId"):
        ArchiveView(env.archive, source_id="other")
    foreign = env.root / "foreign"
    foreign.mkdir()
    with pytest.raises(ArchiveError, match="no ownership marker"):
        ArchiveView(foreign)
    assert not os.listdir(foreign)


def _version(stored_path: str, data: bytes) -> ArchiveVersion:
    return ArchiveVersion(
        relative_path="x",
        stored_path=stored_path,
        sha256=sha(data),
        byte_count=len(data),
        captured_at="",
        run_id="",
        record_id="",
        content_encoding="gzip",
        content_sha256=None,
        content_byte_count=None,
        previous_sha256=None,
        previous_relation=None,
        record={},
    )


@pytest.mark.parametrize(
    "stored_path",
    [
        "../outside.jsonl",
        "a/../../outside.jsonl",
        r"..\outside.jsonl",
        "C:/outside.jsonl",
        "//server/share/x",
        "snapshots/./x",
        "/abs",
    ],
)
def test_view_refuses_stored_paths_outside_the_root(env: Env, stored_path: str) -> None:
    write(env.source / ROLL, b"{}\n")
    env.configure()
    env.capture()
    view = ArchiveView(env.archive)
    with pytest.raises(ArchiveError, match="unsafe stored path"):
        view.stored_file(_version(stored_path, b"x"))


def test_view_reports_corrupt_gzip_as_archive_error(env: Env) -> None:
    write(env.source / ROLL, b"{}\n")
    env.configure()
    env.capture()
    corrupt = bytearray(gzip.compress(b'{"line":1}\n' * 100))
    corrupt[20:30] = b"\xff" * 10
    write(env.archive / "snapshots" / "manual" / "bad.jsonl.gz", bytes(corrupt))
    view = ArchiveView(env.archive)
    with pytest.raises(ArchiveError, match="cannot decode"):
        view.read_content(_version("snapshots/manual/bad.jsonl.gz", bytes(corrupt)))


def test_view_reports_truncated_zstd_as_archive_error(env: Env) -> None:
    write(env.source / ROLL, b"{}\n")
    env.configure()
    env.capture()
    truncated = zstandard.ZstdCompressor().compress(os.urandom(5000))[:2000]
    write(env.archive / "snapshots" / "manual" / "bad.jsonl.zst", truncated)
    view = ArchiveView(env.archive)
    version = _version("snapshots/manual/bad.jsonl.zst", truncated)
    with pytest.raises(ArchiveError, match="cannot decode"):
        view.read_content(version)
    two = zstandard.ZstdCompressor().compress(b"a\n") + zstandard.ZstdCompressor().compress(b"b\n")
    write(env.archive / "snapshots" / "manual" / "two.jsonl.zst", two)
    assert view.read_content(_version("snapshots/manual/two.jsonl.zst", two)) == b"a\nb\n"
