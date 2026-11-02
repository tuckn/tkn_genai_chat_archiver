from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tkn_genai_chat_archiver.cli import main
from tkn_genai_chat_archiver.config import ConfigError, load_config
from tkn_genai_chat_archiver.patterns import PatternSet

from .conftest import Env, write


def test_patterns_select_and_prune() -> None:
    patterns = PatternSet(
        ["antigravity*/brain/**", "antigravity*/*.pb"], ["**/presence/**", "**/*.lock"], case_insensitive=True
    )
    assert patterns.selects("antigravity-cli/brain/x/.system_generated/logs/transcript.jsonl")
    assert patterns.selects("antigravity/agyhub_summaries_proto.pb")
    assert not patterns.selects("antigravity/bin/webm_encoder.exe")
    assert not patterns.selects("antigravity/brain/x/a.lock")
    assert patterns.may_contain("antigravity")
    assert patterns.may_contain("antigravity/brain")
    assert not patterns.may_contain("antigravity/bin")
    assert not patterns.may_contain("antigravity/brain/presence")
    workspace = PatternSet(
        ["workspaceStorage/*/chatSessions/**", "workspaceStorage/*/workspace.json"], [], case_insensitive=True
    )
    assert workspace.may_contain("workspaceStorage/abc")
    assert workspace.may_contain("workspaceStorage/abc/chatSessions")
    assert not workspace.may_contain("workspaceStorage/abc/ms-python.python")
    assert workspace.selects("workspaceStorage/abc/workspace.json")


def _write_config(path: Path, targets: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump({"schema_version": "1.0.0", "targets": targets}), encoding="utf-8")


def test_config_rejects_overlapping_archive_roots(tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    common = {"profile": "codex", "source_root": str(tmp_path / "s"), "device_id": "pc"}
    _write_config(
        config,
        {
            "a": {**common, "archive_root": str(tmp_path / "archive")},
            "b": {**common, "archive_root": str(tmp_path / "archive" / "nested")},
        },
    )
    with pytest.raises(ConfigError, match="one writer"):
        load_config(config)


def test_config_requires_device_id_and_known_profile(tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    _write_config(config, {"a": {"profile": "codex", "archive_root": str(tmp_path / "a")}})
    with pytest.raises(ConfigError, match="device_id"):
        load_config(config)
    _write_config(config, {"a": {"profile": "chatgpt", "archive_root": str(tmp_path / "a"), "device_id": "pc"}})
    with pytest.raises(ConfigError, match="profile"):
        load_config(config)


def test_source_id_defaults_to_archive_folder_name(tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    archive = tmp_path / "Codex" / "chatLogs" / "my-pc-windows"
    _write_config(
        config,
        {
            "a": {
                "profile": "codex",
                "source_root": str(tmp_path / "s"),
                "archive_root": str(archive),
                "device_id": "pc",
            }
        },
    )
    assert load_config(config).targets["a"].source_id == "my-pc-windows"


def test_key_value_output_and_usage_error(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    write(env.source / "sessions" / "a.jsonl", b"x\n")
    env.configure()
    capsys.readouterr()
    code = main(["capture", "--dry-run", "--config", str(env.config_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "command=capture" in out
    assert "targets[0].counts.planned=1" in out
    assert "targets[0].omittedDetailsCount=1" in out
    code = main(["capture", "--no-such-option"])
    out = capsys.readouterr().out
    assert code == 2
    assert "status=failed" in out


def test_config_init_writes_example_once(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "init", "--json"]) == 0
    path = isolated_home / ".tkn" / "genai_chat_archiver" / "config.yaml"
    assert path.is_file()
    assert "C:\\path\\to" in path.read_text(encoding="utf-8")
    capsys.readouterr()
    assert main(["config", "init"]) == 0
    assert "status=unchanged" in capsys.readouterr().out
