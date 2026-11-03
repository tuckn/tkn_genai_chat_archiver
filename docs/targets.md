# 対象アプリごとの取得範囲

> 各アプリの保存形式は公式に文書化されておらず、版によって変わります。ここに書いた構成は、2026年11月にWindows 11上で観察したものです。

## 1. 共通の決まり

- 取得範囲は `source_root` からのglobパターン（`include`・`exclude`）で決めます。`*` は1階層内、`**` は任意の深さに一致します。Windowsでは大文字・小文字を区別しません。`exclude` が `include` より優先します。
- 拡張子では選びません。フォルダを丸ごと対象にし、アプリの本体・キャッシュ・ロックなど、明らかにチャットログでないものだけを除きます。
- 隠しファイル・隠しフォルダ（`.system_generated` など）も対象です。シンボリックリンクとジャンクションはたどりません。
- 認証情報のファイルは、どのprofileにも含めません。
- 中身のないフォルダは保存しません（ファイルがなければ何も作りません）。

## 2. Codex（`codex`）

取得元：`~/.codex`。WSL内のCodexは `\\wsl.localhost\<distro>\home\<user>\.codex` を別のtargetにします。

| 取得する | 内容 |
| --- | --- |
| `sessions/**` | 会話ごとのrollout。新しいものは `.jsonl`、古くなるとCodexが `.jsonl.zst` に圧縮して置き換える |
| `archived_sessions/**` | アーカイブした会話。`.jsonl` または `.jsonl.zst` |
| `session_index.jsonl` | 会話のタイトルの索引 |
| `.codex-global-state.json` | アプリの状態。会話とプロジェクトの対応を含む。使用中は1〜2分ごとに書き換わるため、変わった実行ごとに1版（数MB）増える。Codexは一時ファイルに書いてから置き換えるので、見送りの待ち時間を外している（`settle_exempt`） |
| `history.jsonl` | 入力履歴 |

| 除外する | 理由 |
| --- | --- |
| `**/*.ffs_db` | FreeFileSyncなど、Codex以外のツールが置いた同期用のデータベース |
| `**/*.lock` | ロックファイル |

取得しないもの：`state_*.sqlite`（会話一覧などの索引。数十MBあり毎日変わる）、`thread_history_*.sqlite`（rolloutから作られる投影。数百MB）、`logs_*.sqlite`、`memories*`、`sqlite/`、`auth.json`（認証情報）、`config.toml`、`.sandbox*`、`cache/`、`.tmp/`、`tmp/`。必要になれば `include` に追加できます。SQLiteは3.3節の方法で取ります。

圧縮について：Codexは `x.jsonl` を `x.jsonl.zst` に置き換えます。両方とも別の相対パスの版として保存され、`content.sha256` が同じなら展開後の内容は同じです。元の `x.jsonl` がなくなったことは `sourceMissing` で記録します。

## 3. Claude Code（`claude-code`）

取得元：`~/.claude`。CLI、デスクトップアプリのCodeタブ、IDE拡張のいずれも、この場所に書きます。

| 取得する | 内容 |
| --- | --- |
| `projects/**` | 作業フォルダごとのフォルダ（作業フォルダのパスの記号を `-` に置き換えた名前）。中身は次のとおり |
| ├ `<encoded-cwd>/<session-id>.jsonl` | 会話 |
| ├ `<encoded-cwd>/<session-id>/subagents/**` | サブエージェントの会話 |
| ├ `<encoded-cwd>/<session-id>/tool-results/**` | 本文に収まらない長いツール結果（`.txt`・`.pdf` など） |
| ├ `<encoded-cwd>/<session-id>/custom-title.json` | 会話のタイトル |
| └ `<encoded-cwd>/memory/**` | 自動メモリ |
| `history.jsonl` | 全プロジェクトの入力履歴 |

取得しないもの：`.credentials.json`（認証情報）、`settings.json`、`file-history/`（巻き戻し用の編集前ファイル）、`sessions/`、`session-env/`、`shell-snapshots/`、`paste-cache/`、`plugins/`、`cache/`、`backups/`、`ide/`。

使用中の会話のファイルは書き込みが続くため、見送りになります。作業を終えてから `settle_seconds` が過ぎた後の実行で保存されます。

保持期間について：Claude Codeは、既定では起動時に30日より古い会話を削除します（設定 `cleanupPeriodDays`）。アーカイブ済みの版は消えませんが、取得する前に消えた会話は取り戻せません。長い値を設定してください。

サブエージェントやツール結果のパスは長くなりやすく、保存先によっては260文字を超えます。

## 4. Antigravity（`antigravity`）

取得元：`~/.gemini`。IDE（`antigravity/`）とCLI（`antigravity-cli/`）を1つのtargetでまとめて取ります。

| 取得する | 内容 |
| --- | --- |
| `antigravity*/brain/**` | 会話ごとのフォルダ。`.system_generated/logs/transcript*.jsonl`、ステップの出力、画像、添付ファイル |
| `antigravity*/conversations/**` | 会話ごとのSQLite（`<id>.db`、WALモード）。バックアップ機能で取る |
| `antigravity*/conversation_summaries.db` | 会話一覧（タイトル・プレビュー）のSQLite |
| `antigravity*/annotations/**`、`antigravity*/implicit/**`、`antigravity*/knowledge/**` | 会話への注記など（protobufのテキスト・バイナリ） |
| `antigravity*/*.pb`、`antigravity*/*.pbtxt` | 要約・状態 |
| `antigravity*/history.jsonl` | CLIの入力履歴 |

| 除外する | 理由 |
| --- | --- |
| `**/*.lock`、`**/presence/**` | ロック・起動中の印 |
| `**/*-wal`、`**/*-shm`、`**/*-journal` | SQLiteのバックアップに含まれる |

取得しないもの：`bin/`・`builtin/`・`lib/`・`mcp/`・`sidecar_data/`（アプリ本体と同梱物）、`log/`・`crashes/`・`cli.log`（アプリのログ）、`cache/`、`updater/`、`settings.json`、`installation_id`、`~/.gemini/config/`（プラグイン・Skill）。

## 5. GitHub Copilot（`github-copilot`）

GitHub Copilotは、使う場所によって会話を2か所に保存します。1つのtargetで両方を取り、保存先の中を取得元の名前（`vscode/`・`cli/`）で分けます（[データ契約](data-contract.md)の「複数の取得元」）。

| 取得元の名前 | 既定の場所 | 書き込むもの |
| --- | --- | --- |
| `vscode` | `%APPDATA%\Code\User` | VS CodeのCopilot Chat |
| `cli` | `~/.copilot`（環境変数 `COPILOT_HOME` で変えている場合はその場所） | GitHub Copilot CLI。VS Codeの「エージェントのセッション」に出るCopilot CLIのセッションもここ |

VS Code Insidersを使う場合は、`source_roots` に `vscode-insiders: "%APPDATA%\\Code - Insiders\\User"` を足します（取得範囲は `vscode*/` で始まるので、そのまま当てはまります）。片方の取得元がない（例：CLIを入れていない）場合は、警告を出して残りを取ります。

VS Codeのチャット欄の履歴はVS Code本体の領域に保存され、Copilot Chatの拡張機能も自分の領域に会話の記録を置きます。一方、CodexやClaude CodeのVS Code拡張は、それぞれ `~/.codex`・`~/.claude` の会話を表示するだけで、VS Codeの領域には保存しません。そのため、VS Codeの領域のチャットは、実質的にCopilot Chatの会話です。

| 取得する | 内容 |
| --- | --- |
| `vscode*/workspaceStorage/*/chatSessions/**` | ワークスペースごとのチャット（`.jsonl`、古い版は `.json`）。チャット欄を開いただけで、発言のない空のセッションもできる |
| `vscode*/workspaceStorage/*/chatEditingSessions/**` | チャットからの編集の記録 |
| `vscode*/workspaceStorage/*/GitHub.copilot-chat/transcripts/**` | Copilot Chatの会話の記録 |
| `vscode*/workspaceStorage/*/GitHub.copilot-chat/chat-session-resources/**` | 本文に収まらない長いツール結果 |
| `vscode*/workspaceStorage/*/GitHub.copilot-chat/debug-logs/**` | セッションごとの処理の記録（`models.json` を除く） |
| `vscode*/workspaceStorage/*/workspace.json` | そのフォルダ名（ハッシュ）がどのワークスペースかを示す。同じフォルダに保存するチャットがある場合だけ取る（`companion_include`） |
| `vscode*/globalStorage/emptyWindowChatSessions/**` | フォルダを開いていないウィンドウでのチャット |
| `vscode*/globalStorage/github.copilot-chat/session-store.db` | Copilot Chatのセッションの索引（SQLite） |
| `cli/session-state/**` | 会話ごとのフォルダ（`events.jsonl`・`workspace.yaml`・`session.db`・チェックポイント・巻き戻し用の記録） |
| `cli/history-session-state/**` | 古い版のCLIの会話（1会話1つのJSON） |
| `cli/session-store.db`・`cli/data.db` | 会話一覧・検索用の索引と、アプリの状態（SQLite） |
| `cli/attachments/**` | 会話に貼り付けた画像など |
| `cli/command-history-state.json` | 入力履歴 |
| `cli/sidebar-sessions-state/**`・`cli/vscode.session.metadata.cache.json` | VS Codeとの連携で使うセッションの情報 |

| 除外する | 理由 |
| --- | --- |
| `**/*.lock`、`**/.session-operation-locks/**` | ロック |
| `**/debug-logs/*/models.json` | 使えるモデルの一覧（1つ約54 KB）で、会話ではない |
| `**/*-wal`、`**/*-shm`、`**/*-journal` | SQLiteのバックアップに含まれる |

取得しないもの：`workspaceStorage/*/state.vscdb`（ワークスペースのUI状態全体）、`workspaceStorage/*/GitHub.copilot-chat/codebase-external.sqlite`（コードの索引）、`globalStorage/github.copilot-chat/` のその他（埋め込みのキャッシュ・同梱のエージェント定義など）、`~/.copilot` の `logs/`・`config.json`・`settings.json`・`ide/`・`hooks/`・`run/`・`repo-metadata-cache.db`・`data.db.pre-update-backup-*`（更新前にアプリが作るDBのコピー）。

Visual StudioやJetBrains IDEのCopilotの保存場所は、まだ調べていません。

### 5.1. `vscode-chat`（旧profile）

VS Codeの領域だけを取る最初のprofileです。`GitHub.copilot-chat/` の会話の記録を含まず、`workspace.json` も全ワークスペースの分を取ります。新しく使う場合は `github-copilot` を使ってください。すでにこのprofileで作ったアーカイブを読むために残しています。

## 6. 今後の候補

| 候補 | 取得元 | 内容 |
| --- | --- | --- |
| Visual Studio・JetBrains IDEのCopilot | — | 保存場所は未調査 |
| Ollama | — | 既定ではチャット履歴をファイルに残さない |
| ChatGPT Web・claude.ai | — | クラウドにだけあるため対象外 |
