# Changelog

## Unreleased

- New read-only library API `tkn_genai_chat_archiver.view.ArchiveView` for downstream tools: it reads finished manifests only, orders versions by `capturedAt` and `runId`, checks stored size and SHA-256, decodes zstd and gzip and checks the decoded content hash. It writes nothing and takes no lock.
- `scripts/register-scheduled-task.ps1`: the default task name is now `TknGenAIChatArchiver_Daily` and the default time 02:00. New `-TaskPath` parameter registers the task in a Task Scheduler folder (default: the root folder).

## 0.2.0

- New profile `github-copilot`: GitHub Copilot Chat in VS Code (`vscode`) and GitHub Copilot CLI (`cli`) in one archive. It adds Copilot Chat's own transcripts, long tool results and session debug logs, which `vscode-chat` did not capture.
- Targets can read several named source folders (`source_roots`). Their relative paths start with the folder name, and version records carry `sourceRootName`. A missing folder is a warning while another folder is available.
- `companion_include`: files such as VS Code's `workspace.json` are stored only when their folder holds other stored files.
- `vscode-chat` is kept for existing archives and superseded by `github-copilot`.

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
