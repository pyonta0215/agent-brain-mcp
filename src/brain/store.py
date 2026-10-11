"""Entry の保存先。本番は DynamoDB 1 テーブル、テストはメモリ。

テーブル設計（pk / sk と 2 つの GSI）:
  ENTRY#<id>      META          現在の版。gsi1pk=ENTRY（全件）、gsi2pk=PROJECT#<p>（案件別）
  ENTRY#<id>      REV#000001    各版の全文。編集履歴と監査を兼ねる。消さない
  STATE#<project> STATE         Current State（Goal / Next / Blockers）
Scan は使わない。Query は 1MB で打ち切られるため、必ず最後のページまで辿る。
"""

from __future__ import annotations

import copy
from typing import Any, Iterable

META = "META"
GSI_ALL = "gsi1"
GSI_PROJECT = "gsi2"


class ConflictError(Exception):
    """他の書き込みと衝突した（expected_rev が古い、または同じ id が既にある）。"""


def meta_keys(entry: dict[str, Any]) -> dict[str, str]:
    keys = {
        "pk": f"ENTRY#{entry['id']}",
        "sk": META,
        "gsi1pk": "ENTRY",
        "gsi1sk": f"{entry['kind']}#{entry['id']}",
    }
    if entry.get("project"):
        keys["gsi2pk"] = f"PROJECT#{entry['project']}"
        keys["gsi2sk"] = f"{entry.get('created_at', '')}#{entry['id']}"
    return keys


def rev_item(entry: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "pk": f"ENTRY#{entry['id']}",
        "sk": f"REV#{int(entry['rev']):06d}",
        "rev": int(entry["rev"]),
        "at": entry["updated_at"],
        "by": entry["updated_by"],
        "reason": reason,
        "snapshot": copy.deepcopy(entry),
    }


_KEY_ATTRS = ("pk", "sk", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk")


def strip_keys(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in _KEY_ATTRS}


class MemoryStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get(self, entry_id: str) -> dict[str, Any] | None:
        item = self.items.get((f"ENTRY#{entry_id}", META))
        return strip_keys(copy.deepcopy(item)) if item else None

    def all_entries(self) -> list[dict[str, Any]]:
        return [strip_keys(copy.deepcopy(v)) for (pk, sk), v in sorted(self.items.items())
                if sk == META and v.get("gsi1pk") == "ENTRY"]

    def kind_entries(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.all_entries() if e.get("kind") == kind]

    def project_entries(self, project: str) -> list[dict[str, Any]]:
        rows = [v for (pk, sk), v in self.items.items() if sk == META and v.get("gsi2pk") == f"PROJECT#{project}"]
        return [strip_keys(copy.deepcopy(v)) for v in sorted(rows, key=lambda v: v["gsi2sk"])]

    def history(self, entry_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(v) for (pk, sk), v in sorted(self.items.items())
                if pk == f"ENTRY#{entry_id}" and sk.startswith("REV#")]

    def commit(self, changes: Iterable[tuple[dict[str, Any], int | None]], reason: str) -> None:
        changes = list(changes)
        for entry, expected in changes:
            current = self.items.get((f"ENTRY#{entry['id']}", META))
            if expected is None and current is not None:
                raise ConflictError(f"id が既にある: {entry['id']}")
            if expected is not None and (current is None or int(current["rev"]) != expected):
                raise ConflictError(f"版が古い: {entry['id']}")
        for entry, _ in changes:
            self.items[(f"ENTRY#{entry['id']}", META)] = {**copy.deepcopy(entry), **meta_keys(entry)}
            rev = rev_item(entry, reason)
            self.items[(rev["pk"], rev["sk"])] = rev

    def get_state(self, project: str) -> dict[str, Any] | None:
        item = self.items.get((f"STATE#{project}", "STATE"))
        return strip_keys(copy.deepcopy(item)) if item else None

    def put_state(self, project: str, state: dict[str, Any]) -> None:
        self.items[(f"STATE#{project}", "STATE")] = {**copy.deepcopy(state), "pk": f"STATE#{project}", "sk": "STATE"}

    def delete_state(self, project: str) -> None:
        self.items.pop((f"STATE#{project}", "STATE"), None)


# ---- DynamoDB ----------------------------------------------------------------

def to_attr(value: Any) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, (list, tuple)):
        return {"L": [to_attr(v) for v in value]}
    if isinstance(value, dict):
        return {"M": {str(k): to_attr(v) for k, v in value.items()}}
    raise TypeError(f"保存できない型: {type(value).__name__}")


def from_attr(attr: dict[str, Any]) -> Any:
    (kind, value), = attr.items()
    if kind == "S":
        return value
    if kind == "N":
        return int(value) if "." not in value else int(float(value))
    if kind == "BOOL":
        return bool(value)
    if kind == "NULL":
        return None
    if kind == "L":
        return [from_attr(v) for v in value]
    if kind == "M":
        return {k: from_attr(v) for k, v in value.items()}
    if kind == "SS":
        return list(value)
    raise TypeError(f"読めない型: {kind}")


def to_item(data: dict[str, Any]) -> dict[str, Any]:
    return {k: to_attr(v) for k, v in data.items()}


def from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {k: from_attr(v) for k, v in item.items()}


class DynamoStore:
    def __init__(self, client: Any, table: str) -> None:
        self.client = client
        self.table = table

    def _query_all(self, **kwargs: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = None
        while True:
            params = {"TableName": self.table, **kwargs}
            if start:
                params["ExclusiveStartKey"] = start
            page = self.client.query(**params)
            out.extend(from_item(i) for i in page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start:
                return out

    def get(self, entry_id: str) -> dict[str, Any] | None:
        res = self.client.get_item(
            TableName=self.table, Key=to_item({"pk": f"ENTRY#{entry_id}", "sk": META}), ConsistentRead=True
        )
        return strip_keys(from_item(res["Item"])) if res.get("Item") else None

    def all_entries(self) -> list[dict[str, Any]]:
        rows = self._query_all(
            IndexName=GSI_ALL,
            KeyConditionExpression="gsi1pk = :p",
            ExpressionAttributeValues={":p": {"S": "ENTRY"}},
        )
        return [strip_keys(r) for r in rows]

    def kind_entries(self, kind: str) -> list[dict[str, Any]]:
        """gsi1sk は `<kind>#<id>` なので、種類ごとに読める（全件を読まずに済む）。"""
        rows = self._query_all(
            IndexName=GSI_ALL,
            KeyConditionExpression="gsi1pk = :p AND begins_with(gsi1sk, :k)",
            ExpressionAttributeValues={":p": {"S": "ENTRY"}, ":k": {"S": f"{kind}#"}},
        )
        return [strip_keys(r) for r in rows]

    def project_entries(self, project: str) -> list[dict[str, Any]]:
        rows = self._query_all(
            IndexName=GSI_PROJECT,
            KeyConditionExpression="gsi2pk = :p",
            ExpressionAttributeValues={":p": {"S": f"PROJECT#{project}"}},
        )
        return [strip_keys(r) for r in rows]

    def history(self, entry_id: str) -> list[dict[str, Any]]:
        return self._query_all(
            KeyConditionExpression="pk = :p AND begins_with(sk, :r)",
            ExpressionAttributeValues={":p": {"S": f"ENTRY#{entry_id}"}, ":r": {"S": "REV#"}},
            ConsistentRead=True,
        )

    def commit(self, changes: Iterable[tuple[dict[str, Any], int | None]], reason: str) -> None:
        ops = []
        for entry, expected in changes:
            put: dict[str, Any] = {"TableName": self.table, "Item": to_item({**entry, **meta_keys(entry)})}
            if expected is None:
                put["ConditionExpression"] = "attribute_not_exists(pk)"
            else:
                put["ConditionExpression"] = "rev = :expected"
                put["ExpressionAttributeValues"] = {":expected": {"N": str(expected)}}
            ops.append({"Put": put})
            ops.append({"Put": {
                "TableName": self.table,
                "Item": to_item(rev_item(entry, reason)),
                "ConditionExpression": "attribute_not_exists(pk)",
            }})
        if len(ops) > 100:
            raise ValueError("1回の書き込みは50件まで")
        try:
            self.client.transact_write_items(TransactItems=ops)
        except Exception as error:  # botocore の例外型に依存しない
            if type(error).__name__ == "TransactionCanceledException" or "ConditionalCheckFailed" in str(error):
                raise ConflictError("他の書き込みと衝突した。最新の版を読んでからやり直す") from error
            raise

    def get_state(self, project: str) -> dict[str, Any] | None:
        res = self.client.get_item(
            TableName=self.table, Key=to_item({"pk": f"STATE#{project}", "sk": "STATE"}), ConsistentRead=True
        )
        return strip_keys(from_item(res["Item"])) if res.get("Item") else None

    def put_state(self, project: str, state: dict[str, Any]) -> None:
        self.client.put_item(
            TableName=self.table, Item=to_item({**state, "pk": f"STATE#{project}", "sk": "STATE"})
        )

    def delete_state(self, project: str) -> None:
        self.client.delete_item(TableName=self.table, Key=to_item({"pk": f"STATE#{project}", "sk": "STATE"}))
