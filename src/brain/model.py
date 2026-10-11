"""Brain の記録（Entry）の型・検証・Markdown との相互変換。外部依存なし。

Knowledge / Pattern / Decision / Playbook / Casebook / Memory / Action / Idea を 1 つの Entry 型で持つ。
正本は DynamoDB。Markdown は取り込み元と、非常時の書き出し先としてだけ扱う。
"""

from __future__ import annotations

import re
import secrets as _random
from datetime import date, datetime, timedelta, timezone
from typing import Any

JST = timezone(timedelta(hours=9))

KINDS = ("knowledge", "pattern", "decision", "playbook", "casebook", "memory", "action", "idea")
CURATED_KINDS = ("knowledge", "pattern", "decision", "playbook", "casebook")
STATUSES = ("active", "candidate", "superseded", "deprecated")
CONFIDENCES = ("high", "medium", "low")
OUTCOMES = ("done", "partial", "failed", "abandoned")
# アイデアの段階。やめたものは status も deprecated にして、有効な記録の検索から外す。
IDEA_STAGES = {"open": "active", "done": "active", "dropped": "deprecated"}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,120}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# frontmatter のうち Entry の列として持つもの。これ以外は meta へ入れて失わない。
_LIST_FIELDS = ("tags", "related", "sources", "supersedes", "refs")
_SCALAR_FIELDS = (
    "title", "domain", "category", "scope", "confidence", "last_verified", "review_after",
    "superseded_by", "topic", "outcome", "stage",
)
# 書き出し時の frontmatter の並び。テンプレートの順序に合わせる。
_EXPORT_ORDER = (
    "id", "title", "schema_version", "category", "scope", "scope_ref", "applicability", "type", "domain",
    "tags", "confidence", "status",
)

_KIND_TO_TYPE = {"memory": "observation"}
_TYPE_TO_KIND = {"observation": "memory"}
_KIND_DIR = {
    "pattern": "patterns", "decision": "decisions", "playbook": "playbooks",
    "casebook": "casebook", "memory": "inbox", "idea": "ideas",
}


class EntryError(ValueError):
    """入力が Entry の契約を満たさない。呼び出し側へそのまま返してよい文言だけを持つ。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_jst() -> date:
    return datetime.now(JST).date()


def new_id(kind: str, project: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    return f"{project or 'global'}-{kind}-{stamp}-{_random.token_hex(4)}"


# ---- 要約（一覧と検索結果に出す短文） --------------------------------

def plain_text(markdown: str) -> str:
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[\[([^]|]+)(?:\|([^]]+))?\]\]", lambda m: m.group(2) or m.group(1), text)
    text = re.sub(r"^[#>|*+\-\d.\s]+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`~]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def summarize(body: str, limit: int = 180) -> str:
    match = re.search(r"^##\s+Summary\s*$\n(?P<text>.*?)(?=^##\s+|\Z)", body, flags=re.MULTILINE | re.DOTALL)
    if match:
        source = match.group("text")
    else:
        source = body
        for paragraph in re.split(r"\n\s*\n", body):
            candidate = paragraph.strip()
            if not candidate or candidate.startswith(("#", "|", "```", "- ", "* ", ">")):
                continue
            source = candidate
            break
    plain = plain_text(source)
    return plain if len(plain) <= limit else plain[: limit - 1].rstrip() + "…"


# ---- 検証 --------------------------------------------------------------------

def _as_list(value: Any) -> list:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise EntryError("リストで指定してください")
    return value


def validate(entry: dict[str, Any], known_ids: set[str] | None = None) -> None:
    """保存直前の Entry を検証する。known_ids を渡すと superseded_by の参照先も確認する。"""
    eid = entry.get("id", "")
    if not ID_RE.fullmatch(eid or ""):
        raise EntryError("id は英小文字・数字・ハイフンのみ（2〜121文字）")
    if entry.get("kind") not in KINDS:
        raise EntryError(f"kind は {', '.join(KINDS)} のいずれか")
    if entry.get("status") not in STATUSES:
        raise EntryError(f"status は {', '.join(STATUSES)} のいずれか")
    if not str(entry.get("title", "")).strip():
        raise EntryError("title が必要")
    if entry.get("confidence") and entry["confidence"] not in CONFIDENCES:
        raise EntryError("confidence は high / medium / low")
    if entry.get("project") and not PROJECT_RE.fullmatch(entry["project"]):
        raise EntryError("project は英小文字・数字・ハイフンのみ")
    if entry.get("outcome") and entry["outcome"] not in OUTCOMES:
        raise EntryError(f"outcome は {', '.join(OUTCOMES)} のいずれか")
    for key in ("last_verified", "review_after"):
        value = entry.get(key)
        if value:
            if not DATE_RE.fullmatch(value):
                raise EntryError(f"{key} は YYYY-MM-DD")
            try:
                date.fromisoformat(value)
            except ValueError as error:
                raise EntryError(f"{key} は実在する日付") from error
    successor = entry.get("superseded_by")
    if entry["status"] == "superseded" and not successor:
        raise EntryError("superseded には superseded_by が必要")
    if successor:
        if successor == eid:
            raise EntryError("superseded_by に自分自身は指定できない")
        if entry["status"] != "superseded":
            raise EntryError("superseded_by がある場合 status は superseded")
        if known_ids is not None and successor not in known_ids:
            raise EntryError(f"superseded_by の参照先がない: {successor}")
    if entry["kind"] == "memory" and entry["status"] == "active":
        raise EntryError("memory は candidate のまま保存する。採用するなら kind を knowledge などへ変える")
    if entry["kind"] == "idea":
        stage = entry.get("stage")
        if stage not in IDEA_STAGES:
            raise EntryError(f"idea の stage は {', '.join(IDEA_STAGES)} のいずれか")
        if entry["status"] != IDEA_STAGES[stage]:
            raise EntryError(f"stage が {stage} の idea は status を {IDEA_STAGES[stage]} にする")
    elif entry.get("stage"):
        raise EntryError("stage を持てるのは idea だけ")
    for key in _LIST_FIELDS:
        _as_list(entry.get(key))


def flags(entry: dict[str, Any], today: date | None = None) -> list[str]:
    """検索結果に付ける注意書き。conflict は集合全体を見る必要があるので別で付ける。"""
    today = today or today_jst()
    out = []
    if entry.get("status") in ("superseded", "deprecated"):
        out.append(entry["status"])
    if entry.get("status") == "candidate":
        out.append("candidate")
    review_after = entry.get("review_after")
    if review_after:
        try:
            if date.fromisoformat(review_after) <= today:
                out.append("stale")
        except ValueError:
            out.append("stale")
    elif entry.get("kind") in CURATED_KINDS and not entry.get("meta", {}).get("schema_version"):
        out.append("legacy")
    return out


# ---- Markdown との相互変換 ---------------------------------------------------

def from_markdown(fm: dict[str, Any], body: str, *, at: str, by: str) -> dict[str, Any]:
    """lib_fm.parse の結果を Entry にする。未知の frontmatter は meta に残す。"""
    note_type = str(fm.get("type", "")).strip()
    kind = _TYPE_TO_KIND.get(note_type, note_type)
    status = str(fm.get("status", "")).strip()
    if kind == "memory" and status == "active":
        status = "candidate"
    entry: dict[str, Any] = {
        "id": str(fm.get("id", "")).strip(),
        "kind": kind,
        "status": status,
        "body": body.strip("\n"),
    }
    for key in _SCALAR_FIELDS:
        if fm.get(key) not in (None, ""):
            entry[key] = str(fm[key]).strip()
    for key in _LIST_FIELDS:
        if key in fm:
            entry[key] = list(fm[key]) if isinstance(fm[key], list) else [fm[key]]
    if fm.get("evidence_count") not in (None, ""):
        try:
            entry["evidence_count"] = int(str(fm["evidence_count"]))
        except ValueError:
            entry.setdefault("meta", {})["evidence_count"] = fm["evidence_count"]
    if fm.get("scope") == "project" and fm.get("scope_ref"):
        entry["project"] = str(fm["scope_ref"]).strip()
    handled = {"id", "type", "status", "evidence_count", *_SCALAR_FIELDS, *_LIST_FIELDS}
    meta = {k: v for k, v in fm.items() if k not in handled}
    if meta or entry.get("meta"):
        entry["meta"] = {**entry.get("meta", {}), **meta}
    entry["summary"] = summarize(entry["body"])
    entry.update(created_at=at, updated_at=at, created_by=by, updated_by=by, rev=1)
    return entry


def export_path(entry: dict[str, Any]) -> str:
    kind = entry["kind"]
    if kind == "knowledge":
        return f"knowledge/{entry.get('domain') or 'misc'}/{entry['id']}.md"
    if kind == "action":
        return f"journal/{entry.get('project') or 'global'}/{entry['id']}.md"
    return f"{_KIND_DIR[kind]}/{entry['id']}.md"


def _yaml_scalar(value: Any) -> str:
    return str(value)


def to_markdown(entry: dict[str, Any]) -> str:
    """Entry を frontmatter 付き Markdown にする。lib_fm.parse で読み戻せる形に限る。"""
    fm: dict[str, Any] = {}
    fm["id"] = entry["id"]
    fm["title"] = entry["title"]
    fm["type"] = _KIND_TO_TYPE.get(entry["kind"], entry["kind"])
    status = entry["status"]
    if entry["kind"] == "memory" and status == "candidate":
        status = "active"
    fm["status"] = status
    for key in _SCALAR_FIELDS:
        if entry.get(key) not in (None, "") and key != "title":
            fm[key] = entry[key]
    for key in _LIST_FIELDS:
        if key in entry:
            fm[key] = entry[key]
    if "evidence_count" in entry:
        fm["evidence_count"] = entry["evidence_count"]
    for key, value in (entry.get("meta") or {}).items():
        fm.setdefault(key, value)
    if entry.get("project") and "scope_ref" not in fm and entry["kind"] != "action":
        fm["scope_ref"] = entry["project"]
    if entry["kind"] == "action":
        fm.setdefault("project", entry.get("project", ""))
        fm.setdefault("created_by", entry.get("created_by", ""))
        fm.setdefault("created_at", entry.get("created_at", ""))

    ordered = [k for k in _EXPORT_ORDER if k in fm] + [k for k in fm if k not in _EXPORT_ORDER]
    lines = ["---"]
    for key in ordered:
        value = fm[key]
        if isinstance(value, list):
            if value and all(isinstance(item, dict) for item in value):
                lines.append(f"{key}:")
                for item in value:
                    first = True
                    for k, v in item.items():
                        lines.append(f"  {'- ' if first else '  '}{k}: {_yaml_scalar(v)}")
                        first = False
            else:
                lines.append(f"{key}: [{', '.join(_yaml_scalar(v) for v in value)}]")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + entry.get("body", "").strip("\n") + "\n"
