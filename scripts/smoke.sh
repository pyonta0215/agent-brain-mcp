#!/usr/bin/env bash
# 疎通確認。BRAIN_RESOURCE（McpUrl）/ BRAIN_TOKEN_URL / BRAIN_CLIENT_ID / BRAIN_CLIENT_SECRET を環境変数に置いて実行する。
#   bash scripts/smoke.sh                 # initialize → tools/list → brain_context → brain_search → 匿名401
#   BRAIN_SMOKE_WRITE=1 bash scripts/smoke.sh   # brain_log_action も1件書く（記録に残る）
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

: "${BRAIN_RESOURCE:?BRAIN_RESOURCE（スタック出力 McpUrl）を設定してください}"
PROJECT="${BRAIN_PROJECT:-my-app}"
auth="$(python3 client/brain_auth_header.py --raw)"

rpc() {
  curl -sS -X POST "$BRAIN_RESOURCE" \
    -H "Authorization: Bearer $auth" \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    -d "$1"
}

check() {
  python3 -c 'import json,sys; d=json.load(sys.stdin); r=d.get("result") or {}; assert "error" not in d and not r.get("isError"), d; print("ok:", sys.argv[1])' "$1"
}

rpc '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"brain-smoke","version":"0"}}}' | check initialize
rpc '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | check tools/list
rpc "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"brain_context\",\"arguments\":{\"project\":\"$PROJECT\"}}}" | check brain_context
rpc '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"brain_search","arguments":{"query":"デプロイ 手順"}}}' | check brain_search

if [[ "${BRAIN_SMOKE_WRITE:-}" == "1" ]]; then
  rpc "{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"tools/call\",\"params\":{\"name\":\"brain_log_action\",\"arguments\":{\"project\":\"$PROJECT\",\"summary\":\"smoke.sh の疎通確認\",\"changed\":\"なし\",\"verified\":\"scripts/smoke.sh\",\"outcome\":\"done\"}}}" | check brain_log_action
fi

anon="$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{}' "$BRAIN_RESOURCE")"
[[ "$anon" == "401" ]] || { echo "匿名アクセスが401ではなく $anon" >&2; exit 1; }
echo "ok: anonymous 401"
