# AGENTS.md

## Rules of this repository

- This CLI never calls a generative model, never parses chat content beyond file headers, and never writes to,
  moves or deletes anything in a source folder.
- Stored versions and finished manifests are never replaced or deleted. New behavior must keep the
  [data contract](docs/data-contract.md) and add a test that shows existing archive files stay unchanged.
- README, docs, tests and sample configuration must not contain real personal paths or computer names. Use
  placeholders such as `C:\path\to\...` and `my-pc-windows`.
- Tests use temporary folders and synthetic logs only, never real application logs.

## Checks

```shell
uv run python -m ruff format src tests
uv run python -m ruff check .
uv run python -m mypy src/tkn_genai_chat_archiver
uv run python -m pytest
```

## Test temporary directories

- Prefer pytest's `tmp_path`. If Windows ACLs or the sandbox prevent its use, use a fresh dedicated directory
  under `.local/tmp/<tool>/<run-id>/` and remove it after the run.
