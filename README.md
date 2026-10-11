# agent-brain-mcp

[![CI](https://github.com/pyonta0215/agent-brain-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/pyonta0215/agent-brain-mcp/actions/workflows/ci.yml)

複数の AI エージェント（Claude Code / Codex / claude.ai など）が**同じ記憶を読み書きする** MCP サーバのテンプレートです。
AWS の Lambda・DynamoDB・Cognito だけで動き、使わない時間の費用はほぼかかりません。

筆者が日々使っている共有記憶「Brain」から、記憶サーバの部分だけを切り出したものです。

## 何ができるか

- 作業を始めたエージェントが `brain_context` を1回呼ぶと、その案件の「次にやること」・直近の作業（失敗が先）・有効な設計判断・持ち主の判断基準が返る
- 設計判断は「採らなかった案と理由」なしでは保存できない
- 古くなった記録・食い違う記録・置き換えられた記録には、検索結果で `stale` / `conflict` / `superseded` の印が付く
- エージェントが書いた学びはまず「候補」になり、根拠があるときだけ昇格する
- 記録は消えない。すべての版が、どのエージェントがいつ何の理由で変えたかと一緒に残る
- 書き込みはすべて秘密情報・個人情報の検査を通る（`src/brain/guard.py`）

## しくみ

```text
claude.ai のカスタムコネクタ                     Claude Code / Codex / AWS 上のエージェント
  │ OAuth Code + PKCE（Cognito で人がログイン）    │ client_credentials（エージェントごとのクライアント）
  ▼ Authorization: Bearer <access token>           ▼
API Gateway HTTP API  /mcp           ── JWT authorizer（発行元・クライアント・scope）
                      /.well-known/* ── OAuth の自動検出文書（認証なし）
  ▼
Lambda src/handler.py  ── token_use・client_id・scope・aud を再確認。エージェント名は client_id で決まり、入力では受けない
  ▼
DynamoDB 1テーブル（記録の現在の版 / 全版の履歴 / 案件の State）
```

- MCP は Streamable HTTP のステートレス実装（JSON-RPC。SSE は使わない）
- 検索は文字 bigram の IDF。埋め込みや外部の検索サービスは使わない
- Lambda の権限は GetItem / Query / PutItem だけ。Scan と Delete は持たない
- テーブルは削除保護・PITR・`DeletionPolicy: Retain`。スタックを消しても記録は残る

## ツール

| Tool | 権限 | 用途 |
|---|---|---|
| `brain_context` | read | 作業開始時に1回。State・直近の作業・有効な判断・持ち主の判断基準・案件リポジトリで先に読む文書 |
| `brain_search` | read | 判断・調査の前に引く。`flags` に stale / conflict / superseded / candidate |
| `brain_get` | read | 全文と rev。`include_history` で版の履歴 |
| `brain_record_decision` | write | 設計判断。採らなかった案と理由が必須。`supersedes` で置き換え |
| `brain_record_memory` | write | 学びの候補（candidate）。自動では昇格しない |
| `brain_log_action` | write | 作業の終わりに1回。失敗は理由必須。`goal` / `next` / `blockers` で State を更新 |
| `brain_record_idea` | write | アイデア帳。`brain_search` は `kinds` に idea を指定したときだけ返す |
| `brain_update_entry` | write | 昇格・置き換え・使用中止・再検証。`expected_rev` 必須。削除はできない |

## データ

| pk | sk | 内容 |
|---|---|---|
| `ENTRY#<id>` | `META` | 現在の版。`gsi1`（全件）と `gsi2`（案件別、作成順） |
| `ENTRY#<id>` | `REV#000001` | 各版の全文・日時・エージェント・理由 |
| `STATE#<project>` | `STATE` | Goal / Next / Blockers |

`kind`: knowledge / pattern / decision / playbook / casebook / memory / action / idea。
`status`: active / candidate（memory のみ）/ superseded / deprecated。
`domain: user` の knowledge は、`brain_context` が毎回「持ち主の判断基準」として返します。

## デプロイ

AWS CLI・SAM CLI・Python 3.12 以上と、デプロイ権限のある AWS の認証情報が要ります。

```bash
cp projects.example.json src/projects.json   # 任意。案件ごとのリポジトリ・先に読む文書・旧名
BRAIN_AUTH_DOMAIN_PREFIX=my-brain-1234 bash scripts/deploy.sh
```

`BRAIN_AUTH_DOMAIN_PREFIX` は Cognito のログイン画面のドメイン（`<prefix>.auth.<region>.amazoncognito.com`）で、世界で一意にします。
リージョンは `BRAIN_REGION`（既定 `us-east-1`）、スタック名は `BRAIN_STACK`（既定 `AgentBrain`）で変えられます。
スクリプトはテストを通してからデプロイし、匿名の呼び出しが 401 になること、検出文書が 200 を返すことを確かめます。

## 使い始める

### 1. ログインできる人を作る

自己登録はできません。管理者がユーザーを作ります。

```bash
aws cognito-idp admin-create-user --user-pool-id <UserPoolId> --username <あなたのメールアドレス>
```

### 2. 最初の記録を入れる（任意）

`examples/seed/` に、書き戻しの基準（playbook）と判断基準の例（`domain: user`）があります。

```bash
python3 scripts/brain-migrate.py import --src examples/seed --table <TableName>
```

### 3. claude.ai のカスタムコネクタ（claude.ai・スマホ・Claude Code on the web で共通）

claude.ai の設定 → コネクタ → カスタムコネクタを追加。

| 項目 | 値 |
|---|---|
| URL | スタック出力 `McpUrl` |
| OAuth Client ID | スタック出力 `ConnectorClientId` |
| OAuth Client Secret | **空欄**（公開クライアント。PKCE で守る） |

追加すると Cognito のログイン画面が開きます。記録上のエージェント名は `claude-connector` です。

### 4. コネクタを使えないエージェント（Claude Code の CLI / Codex / AWS 上のエージェント）

client_credentials のクライアントを使います。読み書きできる `WriterAgentClientId` と、読むだけの `ReaderAgentClientId` があります。
secret は持ち主が取り出し、各実行環境の secret 設定に置きます（チャットやリポジトリに書かない）。

```bash
aws cognito-idp describe-user-pool-client --user-pool-id <UserPoolId> --client-id <WriterAgentClientId> \
  --query UserPoolClient.ClientSecret --output text
```

`client/brain_auth_header.py` がトークンを取り、期限の5分前まで使い回します（標準ライブラリだけで動きます）。

```bash
export BRAIN_RESOURCE=<McpUrl> BRAIN_TOKEN_URL=<TokenUrl> BRAIN_CLIENT_ID=... BRAIN_CLIENT_SECRET=...
python3 client/brain_auth_header.py          # {"Authorization": "Bearer ..."}
python3 client/brain_auth_header.py --raw    # トークンだけ
```

Claude Code は `.mcp.json` の `headersHelper` にこのスクリプトを指定できます（[examples/claude-code.mcp.json](examples/claude-code.mcp.json)）。
読み取り専用のクライアントでは `BRAIN_SCOPE=<McpUrl>/read` を設定します。
エージェントにいつ何を呼ばせるかは、[examples/AGENTS.snippet.md](examples/AGENTS.snippet.md) を各リポジトリの `AGENTS.md` に貼るのが簡単です。

疎通確認:

```bash
bash scripts/smoke.sh                       # 読むだけ
BRAIN_SMOKE_WRITE=1 bash scripts/smoke.sh   # brain_log_action も1件書く
```

### エージェントを増やす

`infra/template.yaml` の `WriterAgentClient` を複製し、`AGENT_CLIENTS`（client_id → エージェント名）と JWT authorizer の `Audience` に足します。
失効はコネクタならコネクタの削除、機械クライアントならクライアントの削除か secret の再発行です。

## 移行と書き出し

```bash
python3 scripts/brain-migrate.py check  --src notes/                 # Markdown（frontmatter つき）が取り込める形か
python3 scripts/brain-migrate.py import --src notes/ --table <T>     # 取り込み、全件を突き合わせる
python3 scripts/brain-migrate.py export --table <T> --out backup/    # Brain の中身を Markdown へ書き出す
python3 scripts/brain-migrate.py rename-project --table <T> --from old --to new [--apply]
```

## 止める・消す

- 一時的に止める: Lambda の予約同時実行数を 0 にする
- 消す: `aws cloudformation delete-stack --stack-name AgentBrain`。テーブルは残るので、不要なら削除保護を外してから手で消す

## 開発

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

AWS に触らずに動くテストです（DynamoDB の代わりにメモリ上のストアを使います）。CI は Python 3.12 / 3.13 で同じテストと `cfn-lint` を回します。

## ライセンス

MIT
