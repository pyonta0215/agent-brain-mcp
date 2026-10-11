"""アイデア帳。リポジトリを決めずに思いついたことを置き、AI への依頼の起点にする。

MCP の brain_record_idea と、人が使う画面を足す場合はその画面も、同じ関数で記録を作り・直す。
id は `idea-` で始める（画面の IAM をこの接頭辞の項目だけに絞れるように）。
"""

from __future__ import annotations

import secrets as _random
from datetime import datetime, timezone
from typing import Any

from . import model

MAX_TITLE = 200
MAX_BODY = 8000
EDITABLE = ("title", "body", "project", "stage")


def new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    return f"idea-{stamp}-{_random.token_hex(4)}"


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise model.EntryError("title が必要")
    text = " ".join(value.split())
    if len(text) > MAX_TITLE:
        raise model.EntryError(f"title は{MAX_TITLE}文字まで")
    return text


def _body(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise model.EntryError("body は文字列")
    text = value.replace("\r\n", "\n").strip()
    if len(text) > MAX_BODY:
        raise model.EntryError(f"body は{MAX_BODY}文字まで")
    return text


def _project(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not model.PROJECT_RE.fullmatch(value.strip()):
        raise model.EntryError("project は英小文字・数字・ハイフンのみ")
    return value.strip()


def _set_project(entry: dict[str, Any], project: str) -> None:
    if project:
        entry.update(project=project, scope="project")
    else:
        entry.pop("project", None)
        entry.pop("scope", None)


def create(*, title: Any, body: Any = "", project: Any = "", at: str, by: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": new_id(), "kind": "idea", "status": "active", "stage": "open",
        "title": _title(title), "body": _body(body),
        "created_at": at, "updated_at": at, "created_by": by, "updated_by": by, "rev": 1,
    }
    _set_project(entry, _project(project))
    entry["summary"] = model.summarize(entry["body"]) if entry["body"] else ""
    model.validate(entry)
    return entry


def update(entry: dict[str, Any], changes: dict[str, Any], *, at: str, by: str) -> dict[str, Any]:
    """変更後の新しい版を返す（entry 自体は変えない）。"""
    if entry.get("kind") != "idea":
        raise model.EntryError("idea ではない記録は直せない")
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise model.EntryError(f"変更できない項目: {', '.join(sorted(unknown))}")
    out = dict(entry)
    if "title" in changes:
        out["title"] = _title(changes["title"])
    if "body" in changes:
        out["body"] = _body(changes["body"])
        out["summary"] = model.summarize(out["body"]) if out["body"] else ""
    if "project" in changes:
        _set_project(out, _project(changes["project"]))
    if "stage" in changes:
        if changes["stage"] not in model.IDEA_STAGES:
            raise model.EntryError(f"stage は {', '.join(model.IDEA_STAGES)} のいずれか")
        out["stage"] = changes["stage"]
        out["status"] = model.IDEA_STAGES[out["stage"]]
    out.update(rev=int(entry["rev"]) + 1, updated_at=at, updated_by=by)
    model.validate(out)
    return out


def view(entry: dict[str, Any]) -> dict[str, Any]:
    """人が使う画面へ返す形。作成者や meta は出さない。"""
    return {
        "id": entry["id"],
        "title": entry["title"],
        "body": entry.get("body", ""),
        "project": entry.get("project", ""),
        "stage": entry.get("stage", "open"),
        "rev": int(entry.get("rev") or 1),
        "createdAt": entry.get("created_at", ""),
        "updatedAt": entry.get("updated_at", ""),
    }
