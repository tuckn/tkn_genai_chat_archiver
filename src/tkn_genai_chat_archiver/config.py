"""Layered YAML configuration: defaults, user file, working-directory file, explicit file."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .fsutil import IS_WINDOWS, path_is_within
from .patterns import PatternError, PatternSet
from .profiles import PROFILES, Profile

CONFIG_SCHEMA_VERSION = "1.0.0"
SUPPORTED_SCHEMA_MAJOR = "1"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
TOP_LEVEL_KEYS = {"schema_version", "state_root", "logs_root", "reports_root", "defaults", "targets"}
DEFAULT_KEYS = {"settle_seconds", "read_only"}
TARGET_KEYS = {
    "profile",
    "enabled",
    "optional",
    "source_root",
    "archive_root",
    "source_id",
    "device_id",
    "environment",
    "include",
    "exclude",
    "settle_seconds",
    "settle_exempt",
    "read_only",
}


class ConfigError(ValueError):
    def __init__(self, message: str, *, paths: list[str] | None = None) -> None:
        super().__init__(message)
        self.paths = paths or []


def app_home() -> Path:
    return Path.home() / ".tkn" / "genai_chat_archiver"


def user_config_path() -> Path:
    return app_home() / "config.yaml"


def expand_path(text: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(text)))


@dataclass(frozen=True)
class TargetConfig:
    target_id: str
    profile: Profile
    enabled: bool
    optional: bool
    source_root: Path
    archive_root: Path
    source_id: str
    device_id: str
    environment: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    settle_seconds: int
    settle_exempt: tuple[str, ...]
    read_only: bool

    @property
    def patterns(self) -> PatternSet:
        return PatternSet(self.include, self.exclude, case_insensitive=IS_WINDOWS)

    def describe(self) -> dict[str, Any]:
        return {
            "targetId": self.target_id,
            "profile": self.profile.name,
            "software": self.profile.software,
            "enabled": self.enabled,
            "optional": self.optional,
            "sourceRoot": str(self.source_root),
            "archiveRoot": str(self.archive_root),
            "sourceId": self.source_id,
            "deviceId": self.device_id,
            "environment": self.environment,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "settleSeconds": self.settle_seconds,
            "settleExempt": list(self.settle_exempt),
            "readOnly": self.read_only,
        }


@dataclass(frozen=True)
class AppConfig:
    config_paths: tuple[Path, ...]
    state_root: Path
    logs_root: Path
    reports_root: Path
    targets: dict[str, TargetConfig] = field(default_factory=dict)

    def target_state_root(self, target_id: str) -> Path:
        return self.state_root / "targets" / target_id


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"cannot read configuration: {path}: {exc}", paths=[str(path)]) from exc
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in configuration: {path}: {exc}", paths=[str(path)]) from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"configuration must be a mapping: {path}", paths=[str(path)])
    return value


def _merge(base: dict[str, Any], layer: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in layer.items():
        if key == "targets" and isinstance(value, Mapping):
            targets = dict(result.get("targets") or {})
            for target_id, target in value.items():
                previous = targets.get(target_id)
                if isinstance(previous, Mapping) and isinstance(target, Mapping):
                    targets[target_id] = {**previous, **target}
                else:
                    targets[target_id] = target
            result["targets"] = targets
        elif key == "defaults" and isinstance(value, Mapping):
            result["defaults"] = {**(result.get("defaults") or {}), **value}
        else:
            result[key] = value
    return result


def config_layers(explicit: Path | None) -> list[Path]:
    paths: list[Path] = []
    user = user_config_path()
    if user.is_file():
        paths.append(user)
    local = Path.cwd() / ".tkn" / "config.yaml"
    if local.is_file() and local.resolve() != user.resolve():
        paths.append(local)
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"configuration file not found: {explicit}", paths=[str(explicit)])
        paths.append(explicit)
    return paths


def _absolute(value: Any, key: str, where: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty path string")
    path = expand_path(value.strip())
    if "%" in str(path) or "$" in str(path):
        raise ConfigError(f"{where}.{key} contains an undefined environment variable: {value}")
    if not path.is_absolute():
        raise ConfigError(f"{where}.{key} must be an absolute path: {value}")
    return path


def _bool(value: Any, key: str, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} must be true or false")
    return value


def _seconds(value: Any, key: str, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{where}.{key} must be a non-negative integer")
    return int(value)


def _patterns(value: Any, key: str, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{where}.{key} must be a list of glob patterns")
    return tuple(item.strip() for item in value)


def _identifier(value: str, what: str) -> str:
    if not ID_PATTERN.fullmatch(value) or value.endswith(".") or value.lower() in WINDOWS_DEVICE_NAMES:
        raise ConfigError(f"{what} must use ASCII letters, digits, '.', '_' or '-': {value!r}")
    return value


def _target(target_id: str, raw: Any, defaults: Mapping[str, Any]) -> TargetConfig:
    where = f"targets.{target_id}"
    _identifier(target_id, "target id")
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} must be a mapping")
    unknown = sorted(set(raw) - TARGET_KEYS)
    if unknown:
        raise ConfigError(f"{where} has unknown keys: {', '.join(unknown)}")
    profile_name = raw.get("profile")
    if profile_name not in PROFILES:
        raise ConfigError(f"{where}.profile must be one of: {', '.join(sorted(PROFILES))}")
    profile = PROFILES[str(profile_name)]
    source_root = _absolute(raw.get("source_root", profile.default_source_root), "source_root", where)
    archive_root = _absolute(raw.get("archive_root"), "archive_root", where)
    source_id = _identifier(str(raw.get("source_id") or archive_root.name), f"{where}.source_id")
    device_id = raw.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        raise ConfigError(f"{where}.device_id is required (a stable name of the computer)")
    environment = raw.get("environment", "windows" if IS_WINDOWS else "posix")
    if not isinstance(environment, str) or not environment.strip():
        raise ConfigError(f"{where}.environment must be a non-empty string")
    include = _patterns(raw["include"], "include", where) if "include" in raw else profile.include
    exclude = _patterns(raw["exclude"], "exclude", where) if "exclude" in raw else profile.exclude
    exempt = (
        _patterns(raw["settle_exempt"], "settle_exempt", where) if "settle_exempt" in raw else profile.settle_exempt
    )
    try:
        PatternSet(include, exclude, case_insensitive=IS_WINDOWS)
        if exempt:
            PatternSet(exempt, (), case_insensitive=IS_WINDOWS)
    except PatternError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    return TargetConfig(
        target_id=target_id,
        profile=profile,
        enabled=_bool(raw.get("enabled", True), "enabled", where),
        optional=_bool(raw.get("optional", False), "optional", where),
        source_root=source_root,
        archive_root=archive_root,
        source_id=source_id,
        device_id=device_id.strip(),
        environment=environment.strip(),
        include=include,
        exclude=exclude,
        settle_exempt=exempt,
        settle_seconds=_seconds(
            raw.get("settle_seconds", defaults.get("settle_seconds", 600)), "settle_seconds", where
        ),
        read_only=_bool(raw.get("read_only", defaults.get("read_only", True)), "read_only", where),
    )


def _validate_layout(targets: Mapping[str, TargetConfig]) -> None:
    items = list(targets.values())
    for index, target in enumerate(items):
        if path_is_within(target.archive_root, target.source_root) or path_is_within(
            target.source_root, target.archive_root
        ):
            raise ConfigError(f"targets.{target.target_id}: archive_root and source_root must not overlap")
        for other in items[index + 1 :]:
            if path_is_within(target.archive_root, other.archive_root) or path_is_within(
                other.archive_root, target.archive_root
            ):
                raise ConfigError(
                    f"targets.{target.target_id} and targets.{other.target_id} share or nest archive_root; "
                    "each archive root has exactly one writer"
                )
            if other.enabled and path_is_within(target.archive_root, other.source_root):
                raise ConfigError(f"targets.{target.target_id}: archive_root is inside the source of {other.target_id}")


def load_config(explicit: Path | None = None) -> AppConfig:
    paths = config_layers(explicit)
    merged: dict[str, Any] = {}
    for path in paths:
        layer = _read_yaml(path)
        unknown = sorted(set(layer) - TOP_LEVEL_KEYS)
        if unknown:
            raise ConfigError(f"unknown configuration keys in {path}: {', '.join(unknown)}", paths=[str(path)])
        merged = _merge(merged, layer)
    version = merged.get("schema_version", CONFIG_SCHEMA_VERSION)
    if not isinstance(version, str) or version.split(".")[0] != SUPPORTED_SCHEMA_MAJOR:
        raise ConfigError(f"unsupported schema_version {version!r}; expected {CONFIG_SCHEMA_VERSION}")
    defaults = merged.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise ConfigError("defaults must be a mapping")
    unknown_defaults = sorted(set(defaults) - DEFAULT_KEYS)
    if unknown_defaults:
        raise ConfigError(f"defaults has unknown keys: {', '.join(unknown_defaults)}")
    state_root = _absolute(merged.get("state_root", str(app_home() / "state")), "state_root", "config")
    logs_root = _absolute(merged.get("logs_root", str(state_root / "logs")), "logs_root", "config")
    reports_root = _absolute(merged.get("reports_root", str(state_root / "reports")), "reports_root", "config")
    raw_targets = merged.get("targets") or {}
    if not isinstance(raw_targets, Mapping):
        raise ConfigError("targets must be a mapping of target id to settings")
    targets = {str(key): _target(str(key), value, defaults) for key, value in raw_targets.items()}
    lowered = [key.lower() for key in targets]
    if len(set(lowered)) != len(lowered):
        raise ConfigError("target ids must differ by more than letter case")
    _validate_layout(targets)
    return AppConfig(
        config_paths=tuple(paths),
        state_root=state_root,
        logs_root=logs_root,
        reports_root=reports_root,
        targets=targets,
    )


def select_targets(
    config: AppConfig, requested: list[str] | None, *, include_disabled: bool = False
) -> list[TargetConfig]:
    if requested:
        missing = [item for item in requested if item not in config.targets]
        if missing:
            raise ConfigError(f"unknown target: {', '.join(missing)}")
        selected = [config.targets[item] for item in dict.fromkeys(requested)]
        disabled = [item.target_id for item in selected if not item.enabled and not include_disabled]
        if disabled:
            raise ConfigError(f"target is disabled: {', '.join(disabled)}")
        return selected
    return [item for item in config.targets.values() if item.enabled or include_disabled]
