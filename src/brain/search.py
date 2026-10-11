"""文字 bi-gram の IDF 重み付き包含係数による検索（scripts/find-dup.py と同じ採点）。

埋め込みや外部検索は使わない。スコアは候補の並び順にだけ使い、採否は呼び出し側が本文で決める。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable


def norm(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return re.sub(r"[^\wぁ-んァ-ヶ一-龠ー]+", "", text.lower())


def bigrams(text: str) -> set[str]:
    text = norm(text)
    return {text[i:i + 2] for i in range(len(text) - 1)} or {text}


def entry_bigrams(entry: dict[str, Any]) -> set[str]:
    head = (entry.get("title", "") + " " + " ".join(entry.get("tags") or []) + " " + entry.get("topic", "")) * 3
    return bigrams(head + " " + entry.get("body", "")[:3000])


class Index:
    def __init__(self, entries: Iterable[dict[str, Any]]):
        self.entries = list(entries)
        self.grams = [entry_bigrams(e) for e in self.entries]
        self.titles = [bigrams(e.get("title", "")) for e in self.entries]
        df: Counter[str] = Counter()
        for grams in self.grams:
            df.update(grams)
        self.n = max(len(self.entries), 1)
        self.idf = {g: math.log((self.n + 1) / (c + 0.5)) for g, c in df.items()}
        self.default = math.log((self.n + 1) / 0.5)

    def _weight(self, grams: set[str]) -> float:
        return sum(self.idf.get(g, self.default) for g in grams)

    def score(self, query_grams: set[str], doc_grams: set[str]) -> float:
        wq, wd = self._weight(query_grams), self._weight(doc_grams)
        if wq == 0 or wd == 0:
            return 0.0
        return self._weight(query_grams & doc_grams) / min(wq, wd)

    def search(self, query: str, *, keep=lambda e: True, limit: int = 5, minimum: float = 0.0):
        q = bigrams(query)
        # 本文だけの包含係数は短い問い合わせで 1.0 に張り付くため、表題の一致で順位を分ける
        rows = [(self.score(q, g) + 0.2 * self.score(q, t), e)
                for e, g, t in zip(self.entries, self.grams, self.titles) if keep(e)]
        rows = [r for r in rows if r[0] > minimum]
        rows.sort(key=lambda r: (-r[0], r[1].get("id", "")))
        return rows[:limit]
