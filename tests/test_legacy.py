from __future__ import annotations

import json
from pathlib import Path

import zstandard

from .conftest import Env, sha, write

SOURCE_ID = "pc-windows"
REF = "sessions/2026/10/01/rollout-2026-10-01T08-00-00-11111111-1111-1111-1111-111111111111.jsonl"


def make_legacy(env: Env, files: dict[str, bytes]) -> None:
    root = env.archive
    root.mkdir(parents=True)
    marker = {
        "schemaVersion": 1,
        "applicationId": "tkn-genai-chat-note-pipeline",
        "rootKind": "raw",
        "sourceProvider": "codex",
        "sourceId": SOURCE_ID,
    }
    (root / ".tkn-genai-chat-note-root.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
    (root / ".pipeline.lock").write_bytes(b"0")
    lines = []
    for reference, data in files.items():
        write(root.joinpath(*reference.split("/")), data)
        lines.append(
            json.dumps(
                {
                    "schemaVersion": 3,
                    "sourceId": SOURCE_ID,
                    "sourceProvider": "codex",
                    "sourceRef": reference,
                    "captureRef": f"raw:/codex/{SOURCE_ID}/{reference}",
                    "sha256": sha(data),
                    "byteCount": len(data),
                    "capturedAt": "2026-10-13T06:02:07+09:00",
                    "threadId": "11111111-1111-1111-1111-111111111111",
                    "lastEventAt": "2026-10-01T00:00:00Z",
                }
            )
        )
    (root / "manifest.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    state = b'{"thread-project-assignments":{}}'
    write(root / "metadata" / f"{sha(state)}.json", state)


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_adopt_registers_legacy_files_without_changing_them(env: Env) -> None:
    data = b'{"type":"session_meta"}\n'
    make_legacy(env, {REF: data})
    before = snapshot(env.archive)
    env.configure()

    code, result = env.run("legacy", "adopt", "--target", "t1", "--dry-run")
    assert code == 0 and result["targets"][0]["status"] == "planned"
    assert snapshot(env.archive) == before

    code, result = env.run("legacy", "adopt", "--target", "t1")
    assert code == 0, result
    adopted = result["targets"][0]
    assert adopted["status"] == "adopted"
    assert adopted["importedVersions"] == 1 and adopted["metadataSnapshots"] == 1
    after = snapshot(env.archive)
    assert {key: value for key, value in after.items() if key in before} == before
    added = sorted(set(after) - set(before))
    assert added[0] == ".tkn-genai-chat-archive.json"
    assert all(name.startswith("manifests") for name in added[1:])
    versions = env.versions()
    legacy = next(item for item in versions if item["relativePath"] == REF)
    assert legacy["storedPath"] == REF and legacy["captureMethod"] == "legacy-import"
    assert legacy["capturedAt"] == "2026-10-13T06:02:07+09:00"
    app_state = next(item for item in versions if item["relativePath"] == ".codex-global-state.json")
    assert app_state["capturedAtBasis"] == "storedFileModifiedAt"

    code, result = env.run("legacy", "adopt", "--target", "t1")
    assert result["targets"][0]["status"] == "already-adopted"


def test_capture_after_adopt_adds_zst_and_new_sessions(env: Env) -> None:
    data = b'{"type":"session_meta"}\n'
    make_legacy(env, {REF: data})
    write(env.source / (REF + ".zst"), zstandard.ZstdCompressor().compress(data))
    new_ref = REF.replace("11111111-1111", "22222222-2222")
    write(env.source / new_ref, b"new\n")
    env.configure()
    assert env.run("legacy", "adopt", "--target", "t1")[0] == 0

    target = env.capture()

    assert target["counts"]["stored"] == 2
    assert target["contentAlreadyArchivedCount"] == 1
    # The plain .jsonl became .zst, and the app state file is not in this source folder.
    assert target["counts"]["sourceMissing"] == 2
    assert (env.archive / REF).read_bytes() == data


def test_capture_refuses_legacy_root_before_adoption(env: Env) -> None:
    make_legacy(env, {REF: b"x\n"})
    write(env.source / REF, b"x\n")
    env.configure()
    code, result = env.run("capture")
    assert code == 1
    assert "legacy adopt" in result["targets"][0]["error"]


def test_capture_stops_when_legacy_manifest_is_rewritten(env: Env) -> None:
    make_legacy(env, {REF: b"x\n"})
    write(env.source / REF, b"x\n")
    env.configure()
    assert env.run("legacy", "adopt", "--target", "t1")[0] == 0
    manifest = env.archive / "manifest.jsonl"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    code, result = env.run("capture")
    assert code == 1
    assert "changed after adoption" in result["targets"][0]["error"]


def test_adopt_refuses_mismatched_files(env: Env) -> None:
    make_legacy(env, {REF: b"x\n"})
    (env.archive / REF).write_bytes(b"changed\n")
    env.configure()
    code, result = env.run("legacy", "adopt", "--target", "t1")
    assert code == 1
    assert "does not match" in result["targets"][0]["problems"][0]
    assert not (env.archive / "manifests").exists()
    assert not (env.archive / ".tkn-genai-chat-archive.json").exists()


def test_adopt_refuses_other_source_id(env: Env) -> None:
    make_legacy(env, {REF: b"x\n"})
    env.configure(source_id="another-pc")
    code, result = env.run("legacy", "adopt", "--target", "t1")
    assert code == 1
    assert "sourceId" in result["targets"][0]["error"]
