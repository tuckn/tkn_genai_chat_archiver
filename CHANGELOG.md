# Changelog

## 0.1.0

- First release.
- `capture`: scan each target's source folder read-only and store new `(relativePath, sha256)` versions under `snapshots/<runId>/`, recorded in `manifests/<runId>.jsonl`. Stored versions are never replaced or deleted.
- Byte-exact copies for regular files, including application-compressed `.jsonl.zst`; decoded-content SHA-256 for zstd and gzip.
- SQLite databases (detected by header, including WAL mode) are captured with the SQLite backup API.
- Files modified within `settle_seconds`, changed during copy, or locked are deferred to the next run; `settle_exempt` covers files that the application replaces atomically.
- `sourceMissing` records when a previously archived path disappears from the source.
- Interrupted manifests are finalized on the next run when every listed version verifies.
- Profiles: `codex`, `claude-code`, `antigravity`, `vscode-chat`.
- `legacy adopt`: register a tkn-codex-chat-note-pipeline Raw store as `legacy-import` versions without changing its files, and stop later captures if its manifest is rewritten.
- `status`, `verify`, `config init|show|validate`; `key=value` or `--json` results; logs and reports outside the archive.
- `scripts/register-scheduled-task.ps1` for a daily Task Scheduler run.
