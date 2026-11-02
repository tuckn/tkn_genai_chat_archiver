# 移行手順：tkn-codex-chat-note-pipeline のCodexアーカイブを引き継ぐ

`tkn-codex-chat-note-pipeline`（以下、旧pipeline）の `clone`・`pull`・`raw ingest` が作ったCodexのRawアーカイブを、中身を一切変えずにこのCLIへ引き継ぎ、以後の書き込みをこのCLIだけにする手順です。

## 1. 旧pipelineのアーカイブ

旧pipelineのstorage 5（Raw manifest schema 3）は、`raw_root` に次のものを置きます。

| ファイル | 内容 |
| --- | --- |
| `.tkn-genai-chat-note-root.json` | 所有マーカー（`applicationId: tkn-genai-chat-note-pipeline`、`rootKind: raw`、`sourceProvider: codex`、`sourceId`） |
| `.pipeline.lock` | 排他ロック |
| `manifest.jsonl` | 相対パス（`sourceRef`）ごとに最新の1件。内容が変わると、ファイル全体を書き直す |
| `sessions/`、`archived_sessions/` | 取得元と同じ相対パスの、最新のコピー。元が変わると上書きする |
| `metadata/<sha256>.json` | `.codex-global-state.json` のスナップショット（内容のSHA-256をファイル名にする） |

このCLIの規則と違う点：

- `*.jsonl` だけを取るため、Codexが `.jsonl.zst` に圧縮した後の会話を取れない。
- 元が変わるとコピーとmanifestを上書きするため、古い版はアーカイブに残らない。旧pipelineのdata rootの `provenance/blobs/` に、正規化に使った版の正確なコピーが残っていることがある。

## 2. 引き継ぎの考え方

- 旧ファイルは移動・名前変更・書き換え・属性の変更をしない。旧pipelineが作った参照（`raw:/codex/<source_id>/sessions/...`）は、そのまま使える。
- 旧ファイルを `legacy-import` の版としてmanifestに登録する。以後の版は `snapshots/<runId>/` に追加する。
- 同じロックファイル（`.pipeline.lock`）を使うので、旧pipelineと同時には書き込まない。
- 引き継ぎ後に旧pipelineがまた書き込むと、旧ファイルが上書きされ、このCLIの規則が崩れる。このCLIは旧manifestと旧マーカーのSHA-256をマーカーに記録し、毎回照合する。変わっていたら書き込まずに止まり、エラーで知らせる。

## 3. 手順

1. **旧pipelineの書き込みを止める。** 旧pipelineの `scheduled-run`・`clone`・`pull`・`raw ingest` を、その保存先に対して実行しない。定期実行のタスクがあれば無効にする。
2. **設定する。** このCLIの設定に、旧pipelineの `source_root` と `raw_root` を、それぞれ `source_root` と `archive_root` として登録する（profileは `codex`）。`source_id` は旧マーカーの `sourceId` と同じにする。
3. **確認する。**

   ```shell
   tkn-genai-chat-archiver legacy adopt --target codex-my-pc-windows --dry-run
   ```

   旧manifestの件数、各ファイルのSHA-256・サイズの照合結果、manifestにないファイル、アプリ状態のスナップショットの件数を表示する。食い違いが1件でもあれば、引き継ぎはできない。

4. **引き継ぐ。**

   ```shell
   tkn-genai-chat-archiver legacy adopt --target codex-my-pc-windows
   ```

   ロックを取ったうえで全件を照合し、次の2つだけを追加する。

   - `.tkn-genai-chat-archive.json`（`legacyStore` に旧manifest・旧マーカーのSHA-256と件数を記録）
   - `manifests/<runId>.jsonl`（`legacy-import` の版と `run` レコード）

5. **新しい版を取る。**

   ```shell
   tkn-genai-chat-archiver capture --target codex-my-pc-windows --dry-run
   tkn-genai-chat-archiver capture --target codex-my-pc-windows
   ```

   旧pipelineが取っていなかった会話と、`.jsonl.zst` に圧縮された会話が、新しい版として `snapshots/` に入る。`content.sha256` で、旧ファイルと同じ内容かどうかが分かる。

6. **定期実行を登録する。** [README](../README.md#31-定期実行)を参照。

## 4. 旧pipelineを読み取り専用にする（別の変更）

引き継いだ後、旧pipelineがRawを読むには、旧pipeline側の変更が必要です。この変更は旧pipelineのリポジトリで行います。

- Raw取得（`ingest_raw_sources`）を、このCLIのmanifestから「相対パスごとの最新の版」を読む処理に置き換える。`.jsonl.zst` は読むときに展開する。
- `.codex-global-state.json` を `metadata/` に書く代わりに、このCLIが保存した版を読む。
- `raw_root` に `.tkn-genai-chat-archive.json` があれば、Rawへ書き込む処理をすべて止める。

この変更が入るまでは、旧pipelineの書き込みコマンドを実行しないでください。既存のcatalog・レビューが参照する旧ファイルは、そのまま残ります。

## 5. 任意：上書きされた古い版を取り戻す

旧pipelineのdata rootの `provenance/blobs/` には、正規化に使ったRawの版の正確なコピーが残っていることがあります。これを `legacy-import` の版として追加登録すれば、旧アーカイブで上書きされた版の一部を取り戻せます。対象の選び方（Rawの版だけを選び、正規化済みの派生物を除く）は、引き継ぎの後に別途決めます。
