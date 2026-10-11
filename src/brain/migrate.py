"""Markdown ⇄ Brain の移行。取り込み・突き合わせ・書き出しを行う。

取り込みは件数と中身の両方を突き合わせてから完了とする（取りこぼしを黙って成功にしない）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import guard, model
from .frontmatter import parse_text

SOURCE_DIRS = ("knowledge", "patterns", "decisions", "playbooks", "casebook", "inbox")
_VOLATILE = ("created_at", "updated_at", "created_by", "updated_by", "rev")


def fingerprint(entry: dict[str, Any]) -> str:
    stable = {k: v for k, v in entry.items() if k not in _VOLATILE}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_markdown(root: Path, *, at: str, by: str = "import") -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for dirname in SOURCE_DIRS:
        for path in sorted((root / dirname).rglob("*.md")):
            fm, body = parse_text(path.read_text(encoding="utf-8"))
            rows.append((path.relative_to(root).as_posix(), model.from_markdown(fm, body, at=at, by=by)))
    return rows


def check(rows: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """取り込み前の検査。1件でも問題があれば何も書かない。"""
    problems: list[str] = []
    ids = [e["id"] for _, e in rows]
    known = set(ids)
    for rel, entry in rows:
        if ids.count(entry["id"]) > 1:
            problems.append(f"{rel}: id が重複 {entry['id']}")
        try:
            model.validate(entry, known)
        except model.EntryError as error:
            problems.append(f"{rel}: {error}")
        if model.export_path(entry) != rel:
            problems.append(f"{rel}: 書き出し先が一致しない（{model.export_path(entry)}）")
        hits = guard.find({k: v for k, v in entry.items() if k not in _VOLATILE})
        if hits:
            problems.append(f"{rel}: 秘密情報らしき記述 {hits}")
    return sorted(set(problems))


def import_entries(store: Any, rows: list[tuple[str, dict[str, Any]]], *, update: bool = False) -> dict[str, list[str]]:
    report: dict[str, list[str]] = {"created": [], "updated": [], "unchanged": [], "differs": []}
    for _, entry in rows:
        current = store.get(entry["id"])
        if current is None:
            store.commit([(entry, None)], "imported from Markdown")
            report["created"].append(entry["id"])
        elif fingerprint(current) == fingerprint(entry):
            report["unchanged"].append(entry["id"])
        elif update:
            expected = current["rev"]
            entry = {**entry, "rev": expected + 1, "created_at": current["created_at"],
                     "created_by": current["created_by"]}
            store.commit([(entry, expected)], "re-imported from Markdown")
            report["updated"].append(entry["id"])
        else:
            report["differs"].append(entry["id"])
    return report


def verify(store: Any, rows: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Markdown の全件が Brain に同じ中身で入っているか。Brain 側だけにある記録は数だけ示す。"""
    stored = {e["id"]: e for e in store.all_entries()}
    missing = [e["id"] for _, e in rows if e["id"] not in stored]
    differs = [e["id"] for _, e in rows if e["id"] in stored and fingerprint(stored[e["id"]]) != fingerprint(e)]
    extra = sorted(set(stored) - {e["id"] for _, e in rows})
    return {"markdown": len(rows), "brain": len(stored), "missing": missing, "differs": differs,
            "brain_only": len(extra), "ok": not missing and not differs}


def export(entries: list[dict[str, Any]], out: Path) -> list[str]:
    written = []
    for entry in sorted(entries, key=lambda e: e["id"]):
        path = out / model.export_path(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(model.to_markdown(entry), encoding="utf-8")
        written.append(path.relative_to(out).as_posix())
    return written


def rename_project(store: Any, old: str, new: str, *, at: str, by: str = "rename-project",
                   apply: bool = False, batch: int = 25) -> dict[str, Any]:
    """案件の識別子を old から new へ付け替える。id は変えない（参照が切れるため）。

    各記録は project だけを書き換えて版を1つ進める（旧い版は REV# に残る）。Current State は
    new へ写してから old を消す。new に既に State があれば上書きせず止める。apply=False なら数えるだけ。
    """
    for name in (old, new):
        if not model.PROJECT_RE.fullmatch(name):
            raise model.EntryError(f"案件名が不正: {name}")
    if old == new:
        raise model.EntryError("同じ名前には付け替えられない")
    entries = store.project_entries(old)
    old_state = store.get_state(old)
    new_state = store.get_state(new)
    if old_state and new_state:
        raise model.EntryError(f"{new} に Current State が既にある。手で統合してから実行する")
    report = {"from": old, "to": new, "entries": len(entries), "state": bool(old_state), "applied": apply}
    if not apply:
        return report
    changes = []
    for entry in entries:
        updated = {**entry, "project": new, "rev": int(entry["rev"]) + 1, "updated_at": at, "updated_by": by}
        model.validate(updated)
        changes.append((updated, int(entry["rev"])))
    for start in range(0, len(changes), batch):
        store.commit(changes[start:start + batch], f"project renamed: {old} -> {new}")
    if old_state:
        store.put_state(new, {**old_state, "project": new})
        store.delete_state(old)
    return report
