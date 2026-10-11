#!/usr/bin/env python3
"""Brain へ送る Authorization ヘッダを作る（Claude Code の headersHelper 用）。

Cognito の client_credentials でアクセストークンを取り、期限の5分前まで使い回す。
  環境変数: BRAIN_CLIENT_ID / BRAIN_CLIENT_SECRET（必須）
            BRAIN_TOKEN_URL / BRAIN_RESOURCE（必須。スタックの出力 TokenUrl / McpUrl）
            BRAIN_SCOPE（既定: read と write。読み取り専用のクライアントは read だけにする）
  出力:     {"Authorization": "Bearer ..."}（--raw ならトークンだけ。環境変数で渡す Agent 用）

標準ライブラリのみ。secret とトークンはログにも標準エラーにも出さない。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = os.environ.get("BRAIN_TOKEN_URL", "").strip()
RESOURCE = os.environ.get("BRAIN_RESOURCE", "").strip()
MARGIN = 300


def fail(message: str) -> None:
    print(f"brain_auth_header: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    client_id = os.environ.get("BRAIN_CLIENT_ID", "").strip()
    secret = os.environ.get("BRAIN_CLIENT_SECRET", "").strip()
    if not client_id or not secret:
        fail("BRAIN_CLIENT_ID と BRAIN_CLIENT_SECRET を環境変数に設定してください")
    if not TOKEN_URL or not RESOURCE:
        fail("BRAIN_TOKEN_URL と BRAIN_RESOURCE を環境変数に設定してください（infra のスタック出力にある）")
    scope = os.environ.get("BRAIN_SCOPE", f"{RESOURCE}/read {RESOURCE}/write")
    key = hashlib.sha256(f"{client_id}\n{scope}".encode()).hexdigest()[:16]
    cache = os.path.join(tempfile.gettempdir(), f"agent-brain-{os.getuid()}-{key}.json")

    token = None
    try:
        with open(cache, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("expires_at", 0) - MARGIN > time.time():
            token = saved["access_token"]
    except (OSError, ValueError, KeyError):
        pass

    if token is None:
        basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": scope}).encode(),
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as res:
                body = json.load(res)
        except urllib.error.HTTPError as error:
            fail(f"トークンを取得できません（HTTP {error.code}）。client id / secret / scope を確認してください")
        except (urllib.error.URLError, TimeoutError) as error:
            fail(f"{urllib.parse.urlparse(TOKEN_URL).netloc} に接続できません（{type(error).__name__}）。ネットワーク許可を確認してください")
        token = body.get("access_token")
        if not token:
            fail("応答に access_token がありません")
        fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"access_token": token, "expires_at": time.time() + int(body.get("expires_in", 3600))}, f)

    if "--raw" in sys.argv[1:]:
        print(token)
    else:
        print(json.dumps({"Authorization": f"Bearer {token}"}))


if __name__ == "__main__":
    main()
