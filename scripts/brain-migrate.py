#!/usr/bin/env python3
"""Markdown と Brain（DynamoDB）の間で記録を移す。

  python3 scripts/brain-migrate.py check --src notes/                   # 書き込みなし。取り込める形かだけ確かめる
  python3 scripts/brain-migrate.py import --src notes/ --table T        # 未登録の id だけ作り、全件を突き合わせる
  python3 scripts/brain-migrate.py verify --src notes/ --table T        # 全件が同じ中身で入っているか
  python3 scripts/brain-migrate.py export --table T --out backup/       # Brain の中身を Markdown へ書き出す
  python3 scripts/brain-migrate.py rename-project --table T --from OLD --to NEW [--apply]

--src の下の knowledge/ patterns/ decisions/ playbooks/ casebook/ inbox/ にある *.md（frontmatter つき）を読む。
DynamoDB に触るコマンドは、書き込み権限のある AWS の認証情報で実行する。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from brain import migrate, model  # noqa: E402
from brain.store import DynamoStore  # noqa: E402


def store(args):
    import boto3

    return DynamoStore(boto3.client("dynamodb", region_name=args.region), args.table)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "import", "verify", "export", "rename-project"])
    ap.add_argument("--table")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--src", type=Path, help="check / import / verify: Markdown の置き場所")
    ap.add_argument("--update", action="store_true", help="中身が違う既存記録を Markdown で上書きする（版は残る）")
    ap.add_argument("--out", type=Path, default=ROOT / ".scratch" / "brain-export")
    ap.add_argument("--from", dest="old", help="rename-project: 旧い案件名")
    ap.add_argument("--to", dest="new", help="rename-project: 新しい案件名")
    ap.add_argument("--apply", action="store_true", help="rename-project: 付けないと件数を数えるだけ")
    a = ap.parse_args()

    if a.command == "rename-project":
        if not (a.table and a.old and a.new):
            ap.error("rename-project には --table / --from / --to が必要")
        report = migrate.rename_project(store(a), a.old, a.new, at=model.now_iso(), apply=a.apply)
        print(json.dumps(report, ensure_ascii=False))
        return 0

    if a.command == "export":
        if not a.table:
            ap.error("--table が必要")
        written = migrate.export(store(a).all_entries(), a.out)
        print(f"{len(written)}件を書き出しました -> {a.out}")
        return 0

    if not a.src:
        ap.error(f"{a.command} には --src が必要")
    rows = migrate.load_markdown(a.src, at=model.now_iso())
    problems = migrate.check(rows)
    counts: dict[str, int] = {}
    for _, e in rows:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    print(f"Markdown: {len(rows)}件 {json.dumps(counts, ensure_ascii=False)}")
    if problems:
        print("取り込めない記録があります（何も書いていません）:")
        for p in problems:
            print(f"  - {p}")
        return 1
    if a.command == "check":
        print("OK: すべて取り込める形です")
        return 0
    if not a.table:
        ap.error("--table が必要")
    s = store(a)
    if a.command == "import":
        report = migrate.import_entries(s, rows, update=a.update)
        print(json.dumps({k: len(v) for k, v in report.items()}, ensure_ascii=False))
        if report["differs"]:
            print("Brain 側と中身が違う記録（上書きしていません。--update で上書き）:")
            for i in report["differs"]:
                print(f"  - {i}")
            return 1
    result = migrate.verify(s, rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
