from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from tkn_genai_chat_archiver.archive import run_sort_key
from tkn_genai_chat_archiver.cli import main
from tkn_genai_chat_archiver.fsutil import os_path


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return home


def write(path: Path, content: bytes, *, mtime: float | None = None) -> Path:
    os.makedirs(os_path(path.parent), exist_ok=True)
    with open(os_path(path), "wb") as handle:
        handle.write(content)
    stamp = mtime if mtime is not None else 1_700_000_000
    os.utime(os_path(path), (stamp, stamp))
    return path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Env:
    def __init__(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.root = root
        self.capsys = capsys
        self.source = root / "source"
        self.archive = root / "archive" / "Codex" / "chatLogs" / "pc-windows"
        self.state = root / "state"
        self.config_path = root / "config.yaml"
        self.source.mkdir()

    def configure(self, profile: str = "codex", **target: Any) -> None:
        settings: dict[str, Any] = {
            "profile": profile,
            "source_root": str(self.source),
            "archive_root": str(self.archive),
            "device_id": "Test PC",
            "settle_seconds": 0,
        }
        settings.update(target)
        document = {"schema_version": "1.0.0", "state_root": str(self.state), "targets": {"t1": settings}}
        self.config_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    def run(self, *args: str) -> tuple[int, dict[str, Any]]:
        self.capsys.readouterr()
        code = main([*args, "--config", str(self.config_path), "--json", "--full-output"])
        out = self.capsys.readouterr().out
        return code, json.loads(out)

    def capture(self, *extra: str) -> dict[str, Any]:
        code, result = self.run("capture", *extra)
        assert code == 0, result
        target = result["targets"][0]
        assert isinstance(target, dict)
        return target

    def manifests(self) -> list[Path]:
        directory = self.archive / "manifests"
        return sorted(directory.glob("*.jsonl"), key=lambda path: run_sort_key(path.name)) if directory.exists() else []

    def records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in self.manifests():
            out.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        return out

    def versions(self, relative: str | None = None) -> list[dict[str, Any]]:
        return [
            item
            for item in self.records()
            if item["recordType"] == "version" and (relative is None or item["relativePath"] == relative)
        ]


@pytest.fixture
def env(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Env:
    return Env(tmp_path, capsys)


Writer = Callable[..., Path]
