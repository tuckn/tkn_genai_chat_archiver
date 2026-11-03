# データ契約：アーカイブの構成とmanifest

CLI 0.2.0 · manifest・マーカー `schemaVersion` 1.0.0 · 設定 `schema_version` 1.0.0

アーカイブを読む側のツール（検索用の変換、ノート作成など）と、このCLIとの間のファイルの約束を定めます。操作方法は [README](../README.md)、アプリごとの取得範囲は [対象アプリごとの取得範囲](targets.md) を参照してください。

## 1. 用語

| 用語 | 意味 |
| --- | --- |
| target | 設定の1項目。1つ以上の取得元（`source_root` または `source_roots`）と、1つの保存先（`archive_root`）の組 |
| profile | アプリごとの既定の取得範囲（`codex`、`claude-code`、`antigravity`、`github-copilot`、旧 `vscode-chat`） |
| 相対パス（`relativePath`） | 取得元からのパス。区切りは `/`。取得元が複数のtargetでは、先頭に取得元の名前が付く（1.1節） |
| 版（version） | ある相対パスで、ある時点に保存したバイト列。`(relativePath, sha256)` で識別する |
| 実行（run） | 1回の `capture` または `legacy adopt`。`runId` は開始時刻のローカル日時（例：`20261102T030012+0900`） |

### 1.1. 複数の取得元

1つのソフトが複数の場所に会話を書く場合（例：GitHub CopilotのVS Code側とCLI側）、`source_roots` で名前付きの取得元を並べます。このとき、`relativePath` は `<取得元の名前>/<取得元からのパス>`（例：`cli/session-state/<id>/events.jsonl`）になり、版のレコードには `sourceRootName` と、その取得元の `sourceRoot` が入ります。取得元が1つのtarget（`source_root`）では、名前は付きません。

`companion_include` に一致するファイル（例：VS Codeの `workspace.json`）は、同じフォルダの下に保存するほかのファイルがある場合だけ保存します。保存しなかった回も、取得元にあるので `sourceMissing` にはしません。

## 2. アーカイブの構成

```text
<archive-root>/
├── .tkn-genai-chat-archive.json
├── .pipeline.lock
├── manifests/
│   ├── <runId>.jsonl
│   └── <runId>.jsonl.partial
└── snapshots/
    └── <runId>/
        └── <relativePath>
```

- `snapshots/<runId>/<relativePath>`：その実行で保存した版。取得元の相対パスとファイル名をそのまま使います。
- `manifests/<runId>.jsonl`：その実行で記録したレコード。最後の行が `run` レコードのものだけが確定したmanifestです。確定したmanifestは変更しません。
- `manifests/<runId>.jsonl.partial`：実行中のmanifest。中断するとそのまま残ります。次の実行が、載っている版をすべて照合できた場合だけ、`status: "recovered"` の `run` レコードを追記して確定させます。
- 保存する版も失敗もなかった実行では、`manifests/` にも `snapshots/` にも何も作りません。
- 引き継いだ既存アーカイブ（[移行手順](migration-from-codex-chat-note-pipeline.md)）では、既存のファイルが元の場所に残り、`legacy-import` レコードがそれを指します。

### 2.1. 所有マーカー

`.tkn-genai-chat-archive.json`：

```json
{
  "schemaVersion": "1.0.0",
  "applicationId": "tkn-genai-chat-archiver",
  "rootKind": "chatLogArchive",
  "profile": "codex",
  "software": "Codex",
  "sourceId": "my-pc-windows",
  "deviceId": "2023 Example PC",
  "environment": "windows",
  "createdAt": "2026-11-02T03:00:12+09:00",
  "legacyStore": null
}
```

- 書き込む前に、設定の `profile` と `sourceId` がマーカーと一致することを確かめます。一致しなければ止まります。
- 保存先が「存在しない」「空のフォルダ」ならマーカーを作って始めます。マーカーのない空でないフォルダには書き込みません。
- 既存アーカイブを引き継いだ場合、`legacyStore` に引き継ぎ時点の旧manifestと旧マーカーのSHA-256を記録します。以後の `capture` は毎回これを照合し、旧manifestが書き換わっていたら（旧pipelineがまた書き込んだと考えられるため）書き込まずに止まります。

### 2.2. ロック

`.pipeline.lock` の先頭1バイトに、OSが解放する排他ロック（Windowsは `msvcrt.locking`、それ以外は `flock`）を取ります。`tkn-codex-chat-note-pipeline` と同じファイル名・同じ方法なので、両者が同時に同じ保存先へ書くことはありません。ロックを取れなければ、そのtargetは失敗として扱い、ほかのtargetは続けます。

## 3. manifestのレコード

manifestはUTF-8（BOMなし）のJSON Linesです。全レコードに `schemaVersion`、`recordType`、`runId` があります。日時はローカル時刻と時差付きのISO 8601です。

### 3.1. `version`：保存した版

```json
{
  "schemaVersion": "1.0.0",
  "recordType": "version",
  "recordId": "0193f1c2-7a4e-7cde-9a1b-2c3d4e5f6a7b",
  "runId": "20261102T030012+0900",
  "profile": "codex",
  "software": "Codex",
  "sourceId": "my-pc-windows",
  "deviceId": "2023 Example PC",
  "environment": "windows",
  "hostName": "EXAMPLE-PC",
  "sourceRoot": "C:\\Users\\example\\.codex",
  "sourcePath": "C:\\Users\\example\\.codex\\sessions\\2026\\11\\02\\rollout-2026-11-02T08-15-00-<id>.jsonl.zst",
  "relativePath": "sessions/2026/11/02/rollout-2026-11-02T08-15-00-<id>.jsonl.zst",
  "storedPath": "snapshots/20261102T030012+0900/sessions/2026/11/02/rollout-2026-11-02T08-15-00-<id>.jsonl.zst",
  "captureMethod": "copy",
  "capturedAt": "2026-11-02T03:00:15+09:00",
  "byteCount": 402311,
  "sha256": "<64桁の16進>",
  "content": { "encoding": "zstd", "byteCount": 1630112, "sha256": "<64桁の16進>" },
  "sourceStat": { "byteCount": 402311, "modifiedAt": "2026-10-20T06:00:01.120+09:00" },
  "previous": null,
  "pipeline": { "name": "tkn-genai-chat-archiver", "version": "0.1.0" },
  "result": "stored"
}
```

| 項目 | 内容 |
| --- | --- |
| `sourcePath` | 取得時の元ファイルの絶対パス |
| `relativePath` | `sourceRoot` からの相対パス（`/` 区切り）。取得元が複数なら先頭に `sourceRootName` が付く。版の識別に使う |
| `sourceRootName` | 取得元が複数のtargetだけ。その取得元の名前（例：`vscode`、`cli`） |
| `storedPath` | `archive-root` からの保存場所（`/` 区切り） |
| `deviceId`・`environment`・`hostName` | 機器の識別。`deviceId` は設定の安定した名前、`hostName` は取得時に観測したコンピューター名 |
| `captureMethod` | `copy`（バイト列のコピー）、`sqlite-backup`（3.3節）、`legacy-import`（3.5節） |
| `byteCount`・`sha256` | 保存したバイト列のサイズとSHA-256 |
| `content` | 展開後の内容。`encoding` は先頭のマジックナンバーで判定し、`zstd`・`gzip`・`identity`（圧縮なし）のいずれか。`identity` では `sha256` と同じ値になる。展開に失敗した場合は `error` を持ち、版は保存する |
| `sourceStat` | 取得時の元ファイルのサイズと更新日時 |
| `previous` | 同じ `relativePath` の直前の版。`relation` は `appended`（直前の版のバイト列で始まる＝追記）か `rewritten`。最初の版は `null` |
| `result` | `stored`（保存して照合済み）または `imported`（既存アーカイブの登録） |

### 3.2. 版の決め方

- 走査で見つけたファイルの `(relativePath, sha256)` が、過去のどの版とも一致しなければ、新しい版として保存します。一致すれば何もしません（いったん内容が変わって元に戻った場合も保存しません）。
- 前回とサイズ・更新日時が同じファイルはSHA-256を計算せずに「変化なし」とみなします（状態ファイルによる高速化。`--rehash` で無効にできます）。
- 同じ会話が別の形式で残っている場合（例：`x.jsonl` と、Codexが圧縮した `x.jsonl.zst`）、相対パスが違うので別の版として保存します。`content.sha256` が同じなら、展開後の内容は同じです。
- 次のファイルは見送り、次の実行に回します（manifestには載せず、`run` レコードとレポートに残します）。
  - 最終更新から `settle_seconds` 秒が経っていない（`settle_exempt` に一致するファイルを除く。アプリが一時ファイルに書いてから置き換えるため、読み取り中に半端な内容にならないもの）
  - コピーの前後でサイズまたは更新日時が変わった
  - SQLiteがロックされていてバックアップできない

### 3.3. SQLiteのデータベース

- ファイルの先頭16バイトが `SQLite format 3\0` のものをSQLiteとして扱います。拡張子では判定しません。
- 取得元を読み取り専用（`mode=ro`）で開き、SQLiteのバックアップ機能で一時ファイルへ全ページを写してから、通常の版と同じ手順で保存します。WALにだけある変更も含まれます。
- 保存したバイト列は元のファイルと一致しません。元が変わっていなければ、バックアップの結果は毎回同じバイト列になります。
- 同じフォルダの `<name>-wal`・`<name>-shm`・`<name>-journal` は、バックアップに含まれるため個別には保存しません。
- `captureMethod: "sqlite-backup"` のレコードは、次の項目を追加で持ちます。

```json
"sqlite": {
  "libraryVersion": "3.45.3",
  "pageSize": 32768,
  "pageCount": 93,
  "journalMode": "wal",
  "quickCheck": "ok",
  "sourceFiles": [
    { "name": "<id>.db", "byteCount": 3047424, "modifiedAt": "..." },
    { "name": "<id>.db-wal", "byteCount": 0, "modifiedAt": "..." }
  ]
}
```

### 3.4. `sourceMissing`：元のファイルがなくなった

以前に保存した相対パスが取得元から消えたとき、最初に気づいた実行で1回だけ記録します。保存済みの版はそのまま残ります。

```json
{ "schemaVersion": "1.0.0", "recordType": "sourceMissing", "runId": "...", "relativePath": "...", "lastRecordId": "...", "observedAt": "..." }
```

### 3.5. `legacy-import`：引き継いだ既存ファイル

既存アーカイブのファイルを動かさずに版として登録します。`storedPath` は既存の場所（例：`sessions/2026/04/26/rollout-....jsonl`）を指します。

- `capturedAt` は旧manifestの取得日時です。旧manifestにない場合（例：アプリ状態のスナップショット）は、保存ファイルの更新日時を使い、`capturedAtBasis: "storedFileModifiedAt"` を付けます。
- 旧manifestには元の絶対パスがないため、`sourcePath` は設定の `source_root` と相対パスから組み立て、`sourcePathBasis: "reconstructed"` を付けます。
- `legacy` 項目に、旧manifestの行番号・`captureRef`・`threadId`・`lastEventAt` などを残します。

### 3.6. `run`：実行の要約

確定したmanifestの最後の行です。

```json
{
  "schemaVersion": "1.0.0",
  "recordType": "run",
  "runId": "20261102T030012+0900",
  "targetId": "codex-my-pc-windows",
  "status": "complete",
  "startedAt": "...",
  "finishedAt": "...",
  "pipeline": { "name": "tkn-genai-chat-archiver", "version": "0.1.0" },
  "settings": {
    "settleSeconds": 600,
    "include": ["sessions/**", "..."],
    "exclude": ["**/*.lock"],
    "settleExempt": [".codex-global-state.json"],
    "rehash": false
  },
  "counts": { "scanned": 1381, "stored": 229, "unchanged": 1149, "deferred": 2, "excluded": 1, "failed": 0, "sourceMissing": 0 },
  "deferred": [{ "relativePath": "...", "reason": "recently-modified" }],
  "failed": []
}
```

`status` は `complete`、`failed`（保存できなかったファイルがある）、`recovered`（中断した実行を次の実行が確定させた）のいずれかです。

## 4. 読む側が守ること

1. アーカイブに書き込まない。保存したSQLiteは `file:<path>?mode=ro&immutable=1` で開くか、別の場所へコピーしてから開く（そのまま開くと `-wal`・`-shm` が作られ、内容が書き換わるおそれがある）。
2. `manifests/*.jsonl` だけを読む。`.partial` と、最後の行が `run` レコードでないファイルは無視する。
3. 相対パスごとの版は `capturedAt` の順（同じなら `runId` の順）に並べる。最新の版は最後のもの。
4. 使う前に `storedPath` のファイルのサイズとSHA-256を確かめる。
5. 版を参照するときは `(relativePath, sha256)` か `recordId` を使う。`storedPath` は場所であり、識別子ではない。
6. 形式の違う同じ内容は `content.sha256` で見分ける。

## 5. ログとレポート

アーカイブとは別の場所に置きます。

| 種類 | 既定の場所 | 内容 |
| --- | --- | --- |
| ログ | `~/.tkn/genai_chat_archiver/state/logs/<runId>.log` | 進捗・警告・エラー |
| レポート | `~/.tkn/genai_chat_archiver/state/reports/<runId>.json`、`last-run.json` | 標準出力の結果と同じ内容（targetごとの件数、見送り・失敗の一覧、保存先） |
| 状態 | `~/.tkn/genai_chat_archiver/state/targets/<targetId>/stat-cache.json` | 高速化のための前回のサイズ・更新日時・SHA-256。消しても次回に全件を計算し直すだけ |

`--dry-run` ではどれも作りません。
