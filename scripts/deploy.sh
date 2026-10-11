#!/usr/bin/env bash
# Agent Brain をデプロイする。テスト → sam deploy → 匿名401・検出文書200の確認。
#   BRAIN_AUTH_DOMAIN_PREFIX=my-brain-1234 bash scripts/deploy.sh
# 必要: AWS CLI / SAM CLI / python3 / curl と、デプロイ権限のある認証情報。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${BRAIN_REGION:-${AWS_REGION:-us-east-1}}"
STACK="${BRAIN_STACK:-AgentBrain}"

if [[ -z "${BRAIN_AUTH_DOMAIN_PREFIX:-}" ]]; then
  echo "BRAIN_AUTH_DOMAIN_PREFIX（Cognito ログイン画面のドメイン接頭辞。世界で一意）を指定してください。" >&2
  exit 2
fi
for tool in aws sam python3 curl; do
  command -v "$tool" >/dev/null || { echo "$tool が見つかりません。" >&2; exit 2; }
done

echo "==> テスト"
python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py'

if [[ ! -f "$ROOT/src/projects.json" ]]; then
  echo "（src/projects.json がないので、案件設定なしでデプロイします）"
fi

echo "==> デプロイ ($STACK / $REGION)"
sam deploy \
  --template-file "$ROOT/infra/template.yaml" \
  --stack-name "$STACK" \
  --region "$REGION" \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    AuthDomainPrefix="$BRAIN_AUTH_DOMAIN_PREFIX" \
    ${BRAIN_WRITER_AGENT:+WriterAgentName="$BRAIN_WRITER_AGENT"} \
    ${BRAIN_READER_AGENT:+ReaderAgentName="$BRAIN_READER_AGENT"}

output() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
MCP_URL="$(output McpUrl)"
ORIGIN="${MCP_URL%/mcp}"

echo "==> 匿名アクセスが閉じていることを確認"
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' "$MCP_URL")"
[[ "$code" == "401" ]] || { echo "匿名の MCP 呼び出しが401ではなく $code を返しました。" >&2; exit 5; }

echo "==> OAuth の自動検出文書を確認"
for path in .well-known/oauth-protected-resource .well-known/oauth-protected-resource/mcp .well-known/oauth-authorization-server; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$ORIGIN/$path")"
  [[ "$code" == "200" ]] || { echo "/$path が200ではなく $code を返しました。" >&2; exit 5; }
done

cat <<OUT
完了: $MCP_URL

次の手順は README の「使い始める」を参照:
  UserPoolId          $(output UserPoolId)
  ConnectorClientId   $(output ConnectorClientId)
  WriterAgentClientId $(output WriterAgentClientId)
  ReaderAgentClientId $(output ReaderAgentClientId)
  TokenUrl            $(output TokenUrl)
  TableName           $(output TableName)
OUT
