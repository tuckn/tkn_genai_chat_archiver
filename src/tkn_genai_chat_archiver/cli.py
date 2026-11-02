"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import Any, NoReturn

from . import __version__
from .capture import CaptureOptions, capture_target
from .config import AppConfig, ConfigError, load_config, select_targets, user_config_path
from .fsutil import ArchiveError, iso_seconds, json_bytes, now_local, os_path, replace_file, run_id_for, write_new_file
from .inspect_archive import target_status, verify_target
from .legacy import adopt_legacy
from .output import render

LOGGER = logging.getLogger("tkn_genai_chat_archiver")
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _common() -> _Parser:
    parent = _Parser(add_help=False)
    parent.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="explicit configuration file")
    parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print one JSON document")
    parent.add_argument(
        "--full-output", action="store_true", default=argparse.SUPPRESS, help="include every per-file detail"
    )
    verbosity = parent.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS, help="only errors on stderr")
    verbosity.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help="debug output on stderr")
    return parent


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = _Parser(
        prog="tkn-genai-chat-archiver",
        description="Keep byte-exact, versioned snapshots of local generative AI chat logs.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", parser_class=_Parser)

    capture = commands.add_parser("capture", parents=[common], help="store new versions of every enabled target")
    capture.add_argument("--target", action="append", default=None, help="limit to a target id (repeatable)")
    capture.add_argument("--dry-run", action="store_true", help="show the plan without writing anything")
    capture.add_argument("--settle-seconds", type=int, default=None, help="override the recently-modified window")
    capture.add_argument("--rehash", action="store_true", help="ignore the saved size/time cache and hash every file")
    capture.add_argument("--log-dir", type=Path, default=None, help="folder for this run's log file")
    capture.add_argument("--report-dir", type=Path, default=None, help="folder for this run's report")

    status = commands.add_parser("status", parents=[common], help="show archive status (read-only)")
    status.add_argument("--target", action="append", default=None)

    verify = commands.add_parser("verify", parents=[common], help="re-hash stored versions (read-only)")
    verify.add_argument("--target", action="append", default=None)

    legacy = commands.add_parser("legacy", parents=[common], help="adopt an existing store")
    legacy_commands = legacy.add_subparsers(dest="legacy_command", parser_class=_Parser)
    adopt = legacy_commands.add_parser(
        "adopt", parents=[common], help="adopt a tkn-codex-chat-note-pipeline Raw store without changing it"
    )
    adopt.add_argument("--target", required=True)
    adopt.add_argument("--dry-run", action="store_true")
    adopt.add_argument("--log-dir", type=Path, default=None)
    adopt.add_argument("--report-dir", type=Path, default=None)

    config = commands.add_parser("config", parents=[common], help="create, show or validate configuration")
    config_commands = config.add_subparsers(dest="config_command", parser_class=_Parser)
    init = config_commands.add_parser("init", parents=[common], help="write an example user configuration")
    init.add_argument("--force", action="store_true", help="back up and replace an existing configuration")
    show = config_commands.add_parser("show", parents=[common], help="print the effective configuration")
    show.add_argument("--target", action="append", default=None)
    config_commands.add_parser("validate", parents=[common], help="validate the configuration")
    return parser


def _option(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    return getattr(args, name, default)


def _setup_logging(args: argparse.Namespace) -> None:
    level = (
        logging.WARNING
        if _option(args, "quiet", False)
        else logging.DEBUG
        if _option(args, "verbose", False)
        else logging.INFO
    )
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def _add_log_file(path: Path) -> logging.Handler:
    os.makedirs(os_path(path.parent), exist_ok=True)
    handler = logging.FileHandler(os_path(path), encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    return handler


def _write_report(directory: Path, run_id: str, result: dict[str, Any]) -> Path:
    path = directory / f"{run_id}.json"
    counter = 2
    while os.path.exists(os_path(path)):
        path = directory / f"{run_id}-{counter}.json"
        counter += 1
    result["reportPath"] = str(path)
    content = json_bytes(result)
    write_new_file(path, content)
    replace_file(directory / "last-run.json", content)
    return path


def _overall(results: list[dict[str, Any]], ok_states: set[str]) -> tuple[str, int]:
    if any(item.get("status") not in ok_states for item in results):
        return "failed", EXIT_FAILED
    return "success", EXIT_OK


def _run_logged(
    config: AppConfig,
    args: argparse.Namespace,
    command: str,
    run_id: str,
    dry_run: bool,
    body: Any,
) -> dict[str, Any]:
    log_dir = _option(args, "log_dir") or config.logs_root
    report_dir = _option(args, "report_dir") or config.reports_root
    handler: logging.Handler | None = None
    result: dict[str, Any] = {"command": command, "runId": run_id, "dryRun": dry_run}
    if not dry_run:
        log_path = Path(log_dir) / f"{run_id}.log"
        handler = _add_log_file(log_path)
        result["logPath"] = str(log_path)
    try:
        result.update(body())
    finally:
        if handler is not None:
            LOGGER.removeHandler(handler)
            handler.close()
    result["finishedAt"] = iso_seconds(now_local())
    if not dry_run:
        _write_report(Path(report_dir), run_id, result)
    return result


def _cmd_capture(config: AppConfig, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    # A disabled target can still be inspected with an explicit --target and --dry-run.
    targets = select_targets(config, args.target, include_disabled=bool(args.dry_run and args.target))
    if not targets:
        raise ConfigError("no enabled targets in the configuration")
    started = now_local()
    run_id = run_id_for(started)
    if args.settle_seconds is not None and args.settle_seconds < 0:
        raise _UsageError("--settle-seconds must not be negative")
    options = CaptureOptions(
        dry_run=args.dry_run,
        rehash=args.rehash,
        settle_seconds=args.settle_seconds,
        full_output=bool(_option(args, "full_output", False)),
    )

    def body() -> dict[str, Any]:
        results = [capture_target(config, target, options, run_id) for target in targets]
        status, code = _overall(results, {"success", "planned", "skipped"})
        return {"status": status, "exitCode": code, "startedAt": iso_seconds(started), "targets": results}

    result = _run_logged(config, args, "capture", run_id, args.dry_run, body)
    return result, int(result["exitCode"])


def _cmd_adopt(config: AppConfig, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    target = select_targets(config, [args.target], include_disabled=True)[0]
    run_id = run_id_for(now_local())

    def body() -> dict[str, Any]:
        try:
            item = adopt_legacy(target, dry_run=args.dry_run, run_id=run_id)
        except ArchiveError as exc:
            LOGGER.error("%s: %s", target.target_id, exc)
            item = {"targetId": target.target_id, "status": "failed", "error": str(exc)}
        status, code = _overall([item], {"adopted", "planned", "already-adopted"})
        return {"status": status, "exitCode": code, "targets": [item]}

    result = _run_logged(config, args, "legacy adopt", run_id, args.dry_run, body)
    return result, int(result["exitCode"])


def _cmd_status(config: AppConfig, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    targets = select_targets(config, args.target, include_disabled=True)
    results = [target_status(config, target) for target in targets]
    status, code = _overall(results, {"success"})
    return {"command": "status", "status": status, "exitCode": code, "targets": results}, code


def _cmd_verify(config: AppConfig, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    targets = select_targets(config, args.target, include_disabled=True)
    results = [verify_target(target) for target in targets]
    status, code = _overall(results, {"success"})
    return {"command": "verify", "status": status, "exitCode": code, "targets": results}, code


def _cmd_config(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    action = _option(args, "config_command")
    if action is None:
        raise _UsageError("choose a config command: init, show or validate")
    if action == "init":
        path = user_config_path()
        example = resources.files("tkn_genai_chat_archiver").joinpath("resources/config.example.yaml").read_bytes()
        result: dict[str, Any] = {"command": "config init", "configPath": str(path)}
        if os.path.exists(os_path(path)):
            if not args.force:
                result.update(status="unchanged", exitCode=EXIT_OK, message="configuration already exists; use --force")
                return result, EXIT_OK
            backup = path.with_name(f"{path.name}.{run_id_for(now_local())}.bak")
            write_new_file(backup, Path(path).read_bytes())
            result["backupPath"] = str(backup)
        replace_file(path, example)
        result.update(status="created", exitCode=EXIT_OK)
        return result, EXIT_OK
    config = load_config(_option(args, "config"))
    base: dict[str, Any] = {
        "command": f"config {action}",
        "status": "success",
        "exitCode": EXIT_OK,
        "configPaths": [str(path) for path in config.config_paths],
        "stateRoot": str(config.state_root),
        "logsRoot": str(config.logs_root),
        "reportsRoot": str(config.reports_root),
    }
    if action == "show":
        selected = select_targets(config, args.target, include_disabled=True)
        base["targets"] = [target.describe() for target in selected]
    else:
        base["targetIds"] = list(config.targets)
    return base, EXIT_OK


def _dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    command = _option(args, "command")
    if command is None:
        raise _UsageError("choose a command: capture, status, verify, legacy adopt or config")
    if command == "config":
        return _cmd_config(args)
    config = load_config(_option(args, "config"))
    if command == "capture":
        return _cmd_capture(config, args)
    if command == "status":
        return _cmd_status(config, args)
    if command == "verify":
        return _cmd_verify(config, args)
    if command == "legacy":
        if _option(args, "legacy_command") != "adopt":
            raise _UsageError("choose a legacy command: adopt")
        return _cmd_adopt(config, args)
    raise _UsageError(f"unknown command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    as_json = "--json" in raw
    full_output = "--full-output" in raw
    parser = build_parser()
    try:
        args = parser.parse_args(raw)
    except _UsageError as exc:
        print(
            render(
                {"command": None, "status": "failed", "exitCode": EXIT_USAGE, "error": str(exc)},
                as_json=as_json,
                full_output=full_output,
            )
        )
        return EXIT_USAGE
    _setup_logging(args)
    try:
        result, code = _dispatch(args)
    except _UsageError as exc:
        result, code = {"status": "failed", "exitCode": EXIT_USAGE, "error": str(exc)}, EXIT_USAGE
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        result = {
            "status": "failed",
            "exitCode": EXIT_FAILED,
            "errorType": "config",
            "error": str(exc),
            "errorPaths": exc.paths,
        }
        code = EXIT_FAILED
    except KeyboardInterrupt:
        result, code = {"status": "interrupted", "exitCode": EXIT_INTERRUPTED}, EXIT_INTERRUPTED
    except (ArchiveError, OSError) as exc:
        LOGGER.error("%s", exc)
        result, code = (
            {"status": "failed", "exitCode": EXIT_FAILED, "errorType": type(exc).__name__, "error": str(exc)},
            EXIT_FAILED,
        )
    result.setdefault("command", _option(args, "command"))
    print(render(result, as_json=as_json, full_output=full_output))
    return code
