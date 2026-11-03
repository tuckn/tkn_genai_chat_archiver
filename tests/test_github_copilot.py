from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from tkn_genai_chat_archiver.config import ConfigError, load_config

from .conftest import Env, write


def configure(env: Env, roots: dict[str, Path], **extra: object) -> None:
    settings: dict[str, object] = {
        "profile": "github-copilot",
        "source_roots": {name: str(path) for name, path in roots.items()},
        "archive_root": str(env.archive),
        "device_id": "Test PC",
        "settle_seconds": 0,
    }
    settings.update(extra)
    document = {"schema_version": "1.0.0", "state_root": str(env.state), "targets": {"t1": settings}}
    env.config_path.write_text(yaml.safe_dump(document), encoding="utf-8")


def make_sources(env: Env) -> tuple[Path, Path]:
    vscode = env.root / "Code" / "User"
    cli = env.root / "dot-copilot"
    with_chat = vscode / "workspaceStorage" / "aaa"
    write(with_chat / "workspace.json", b'{"folder":"file:///c%3A/repo"}')
    write(with_chat / "chatSessions" / "s1.jsonl", b'{"kind":0}\n')
    write(with_chat / "GitHub.copilot-chat" / "transcripts" / "s1.jsonl", b'{"type":"session.start"}\n')
    write(with_chat / "GitHub.copilot-chat" / "debug-logs" / "s1" / "main.jsonl", b'{"v":1}\n')
    write(with_chat / "GitHub.copilot-chat" / "debug-logs" / "s1" / "models.json", b"[]")
    write(with_chat / "GitHub.copilot-chat" / "codebase-external.sqlite", b"cache")
    write(with_chat / "state.vscdb", b"ui state")
    write(vscode / "workspaceStorage" / "bbb" / "workspace.json", b'{"folder":"file:///c%3A/other"}')
    write(vscode / "globalStorage" / "github.copilot-chat" / "settingEmbeddings.json", b"{}")
    write(cli / "session-state" / "c1" / "events.jsonl", b'{"type":"session.start"}\n')
    write(cli / "session-state" / "c1" / "inuse.123.lock", b"1")
    write(cli / "session-state" / ".session-operation-locks" / "c1.lock", b"")
    write(cli / "logs" / "process.log", b"log")
    write(cli / "config.json", b"{}")
    database = cli / "session-store.db"
    connection = sqlite3.connect(database)
    connection.execute("create table sessions (id text)")
    connection.execute("insert into sessions values ('c1')")
    connection.commit()
    connection.close()
    return vscode, cli


def test_vscode_chat_and_cli_go_to_one_archive_with_root_prefixes(env: Env) -> None:
    vscode, cli = make_sources(env)
    configure(env, {"vscode": vscode, "cli": cli})

    target = env.capture()

    stored = sorted(item["relativePath"] for item in env.versions())
    assert stored == [
        "cli/session-state/c1/events.jsonl",
        "cli/session-store.db",
        "vscode/workspaceStorage/aaa/GitHub.copilot-chat/debug-logs/s1/main.jsonl",
        "vscode/workspaceStorage/aaa/GitHub.copilot-chat/transcripts/s1.jsonl",
        "vscode/workspaceStorage/aaa/chatSessions/s1.jsonl",
        "vscode/workspaceStorage/aaa/workspace.json",
    ]
    events = env.versions("cli/session-state/c1/events.jsonl")[0]
    assert events["sourceRootName"] == "cli"
    assert events["sourceRoot"] == str(cli)
    assert events["storedPath"] == f"snapshots/{target['runId']}/cli/session-state/c1/events.jsonl"
    assert env.versions("cli/session-store.db")[0]["captureMethod"] == "sqlite-backup"
    assert target["sourceRoots"] == {"vscode": str(vscode), "cli": str(cli)}

    again = env.capture()
    assert again["counts"]["stored"] == 0
    assert again["counts"]["sourceMissing"] == 0


def test_workspace_json_follows_its_chat_files(env: Env) -> None:
    vscode, cli = make_sources(env)
    configure(env, {"vscode": vscode, "cli": cli})
    env.capture()
    write(vscode / "workspaceStorage" / "bbb" / "chatSessions" / "s2.jsonl", b'{"kind":0}\n')
    target = env.capture()
    assert {item["relativePath"] for item in target["items"]} == {
        "vscode/workspaceStorage/bbb/chatSessions/s2.jsonl",
        "vscode/workspaceStorage/bbb/workspace.json",
    }


def test_missing_cli_folder_is_a_warning(env: Env) -> None:
    vscode, _cli = make_sources(env)
    configure(env, {"vscode": vscode, "cli": env.root / "no-copilot-cli"})
    target = env.capture()
    assert target["status"] == "success"
    assert target["counts"]["stored"] == 4
    assert "cli" in target["warnings"][0]


def test_source_roots_configuration_is_validated(tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    base = {"profile": "github-copilot", "archive_root": str(tmp_path / "a"), "device_id": "pc"}
    config.write_text(yaml.safe_dump({"targets": {"t": {**base, "source_root": str(tmp_path)}}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="source_roots"):
        load_config(config)
    config.write_text(yaml.safe_dump({"targets": {"t": base}}), encoding="utf-8")
    target = load_config(config).targets["t"]
    assert [root.name for root in target.source_roots] == ["vscode", "cli"]
    assert target.multi_root
