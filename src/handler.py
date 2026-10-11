"""Brain の Lambda 入口（API Gateway HTTP API, payload v2）。

認証は API Gateway の JWT authorizer（Cognito）で済ませたうえで、ここでも
token_use=access・登録済み client_id・scope を確かめる。Agent 名は client_id から決め、入力では受けない。

利用者は2種類:
- Agent（client_credentials）: scope の read / write で権限が決まる
- claude.ai コネクタ（人がログインする Code + PKCE）: User Pool は管理者が作ったユーザーしかログインできないので
  read / write を与える。aud があれば MCP の URL（resource）と一致することを確かめる
OAuth の自動検出（RFC 9728 / RFC 8414）の文書は認証なしで返す。
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from brain import mcp
from brain.store import DynamoStore
from brain.tools import Brain

RESOURCE = os.environ["BRAIN_RESOURCE"]
ORIGIN = RESOURCE.rsplit("/", 1)[0]
AUTH_BASE = os.environ["BRAIN_AUTH_BASE"]
READ_SCOPE = f"{RESOURCE}/read"
WRITE_SCOPE = f"{RESOURCE}/write"
PRM_PATH = "/.well-known/oauth-protected-resource"
AS_PATH = "/.well-known/oauth-authorization-server"
CHALLENGE = {"www-authenticate": f'Bearer resource_metadata="{ORIGIN}{PRM_PATH}"'}
MAX_BODY = 64 * 1024

_CACHE: dict[str, Any] = {}
_STORE = None


def _agents() -> dict[str, str]:
    return json.loads(os.environ.get("AGENT_CLIENTS", "{}"))


def _interactive() -> set[str]:
    return {c for c in os.environ.get("INTERACTIVE_CLIENTS", "").split(",") if c}


def protected_resource_metadata() -> dict[str, Any]:
    return {
        "resource": RESOURCE,
        "authorization_servers": [ORIGIN],
        "scopes_supported": ["openid"],
        "bearer_methods_supported": ["header"],
        "resource_name": "Brain",
    }


def authorization_server_metadata() -> dict[str, Any]:
    """Cognito の発見文書は PKCE 対応を明示しないため、同じエンドポイントを指す文書をここで返す。"""
    return {
        "issuer": ORIGIN,
        "authorization_endpoint": f"{AUTH_BASE}/oauth2/authorize",
        "token_endpoint": f"{AUTH_BASE}/oauth2/token",
        "revocation_endpoint": f"{AUTH_BASE}/oauth2/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["openid"],
    }


def _projects() -> dict[str, Any]:
    """案件ごとの設定（リポジトリ名・先に読む文書・別名）。無ければ空で動く。"""
    path = os.environ.get("BRAIN_PROJECTS_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "projects.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _store():
    global _STORE
    if _STORE is None:
        import boto3  # Lambda の実行環境に同梱されている

        _STORE = DynamoStore(boto3.client("dynamodb"), os.environ["BRAIN_TABLE"])
    return _STORE


def response(status: int, body: Any = None, extra: dict[str, str] | None = None) -> dict[str, Any]:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "x-robots-tag": "noindex, nofollow",
        **(extra or {}),
    }
    return {
        "statusCode": status,
        "headers": headers,
        "body": "" if body is None else json.dumps(body, ensure_ascii=False),
        "isBase64Encoded": False,
    }


def identify(event: dict[str, Any]) -> tuple[str | None, set[str]]:
    """(Agent 名, 実効 scope) を返す。認められなければ (None, 空)。"""
    claims = (((event or {}).get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims", {})
    if claims.get("token_use") != "access":
        return None, set()
    client_id = claims.get("client_id", "")
    agent = _agents().get(client_id)
    if not agent:
        return None, set()
    scopes = set(str(claims.get("scope", "")).split())
    if client_id in _interactive():
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else str(aud or "").strip("[]").split()
        if "openid" not in scopes or (auds and RESOURCE not in auds):
            return None, set()
        return agent, {READ_SCOPE, WRITE_SCOPE}
    return agent, scopes


def handler(event: dict[str, Any], context: Any = None, *, store: Any = None) -> dict[str, Any]:
    log: dict[str, Any] = {"event": "brain_call"}
    try:
        return _handle(event, store, log)
    finally:
        print(json.dumps(log, ensure_ascii=False))


def _handle(event: dict[str, Any], store: Any, log: dict[str, Any]) -> dict[str, Any]:
    http = ((event or {}).get("requestContext") or {}).get("http") or {}
    method = http.get("method", "")
    path = (event or {}).get("rawPath", "")
    log["route"] = f"{method} {path}"

    if method == "GET" and path in (PRM_PATH, f"{PRM_PATH}/mcp"):
        log["status"] = 200
        return response(200, protected_resource_metadata(), {"cache-control": "public, max-age=300"})
    if method == "GET" and path == AS_PATH:
        log["status"] = 200
        return response(200, authorization_server_metadata(), {"cache-control": "public, max-age=300"})

    agent, scopes = identify(event)
    log["agent"] = agent
    if not agent or READ_SCOPE not in scopes:
        log["status"] = 401
        return response(401, {"error": "unauthorized"}, CHALLENGE)
    if path != "/mcp":
        log["status"] = 404
        return response(404, {"error": "not_found"})
    if method != "POST":
        log["status"] = 405
        return response(405, {"error": "method_not_allowed"}, {"allow": "POST"})

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if len(raw.encode("utf-8")) > MAX_BODY:
        log["status"] = 413
        return response(413, mcp._error(None, mcp.INVALID_REQUEST, "リクエストが大きすぎる"))
    try:
        message = json.loads(raw)
    except ValueError:
        log["status"] = 400
        return response(400, mcp._error(None, mcp.PARSE_ERROR, "JSON として読めない"))

    brain = Brain(store or _store(), agent=agent, projects=_projects(), cache=_CACHE)
    try:
        status, body = mcp.handle(message, brain, can_write=WRITE_SCOPE in scopes, log=log)
    except Exception as error:  # 内部の詳細は返さず、種類だけ記録する
        log["status"] = 500
        log["error"] = type(error).__name__
        return response(500, mcp._error(message.get("id") if isinstance(message, dict) else None,
                                        mcp.INTERNAL, "内部エラー"))
    log["status"] = status
    return response(status, body)
