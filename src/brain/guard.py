"""書き込み前の秘密情報検査。scripts/scan-secrets.sh と同じパターンと除外条件を使う。

片方だけ直すと検査が黙って弱くなるため、パターンを変えるときは両方を同時に直し、
brain/tests/test_secrets.py と scripts/test-scan-secrets.sh の両方で陽性サンプルを確認する。
"""

from __future__ import annotations

import re
from typing import Any, Iterator

PATTERNS: tuple[tuple[str, str], ...] = (
    ("AWS Access Key ID", r"(A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}"),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{16,}"),
    ("Anthropic API key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI API key", r"sk-(proj-)?[A-Za-z0-9]{32,}"),
    ("Google API key", r"AIza[0-9A-Za-z_-]{35}"),
    ("Slack token", r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    ("Private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("AWS account id", r"[0-9]{12}"),
    ("JWT", r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."),
    ("Bearer token", r"[Bb]earer [A-Za-z0-9_.=-]{24,}"),
    ("Secret assignment",
     r"""(api[_-]?key|secret|passwd|password|token|credential)["' ]*[:=]["' ]*[A-Za-z0-9/+_-]{16,}"""),
    ("Cookie header", r"[Cc]ookie: *[A-Za-z0-9_-]+=[A-Za-z0-9%._-]{16,}"),
    ("Email address", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ("Phone (JP)", r"0[0-9]{1,4}-[0-9]{1,4}-[0-9]{3,4}"),
    ("Money amount (JP)", r"[0-9]{3,4}万円"),
)
ALLOW = re.compile(
    r"(<[^>]*>|noreply@|users\.noreply\.github\.com|example\.(com|org|jp)|YOUR_|placeholder|\$\{|\$[A-Z_]+|<slug>|<sessionId>|scan-secrets)"
)
_COMPILED = tuple((name, re.compile(pattern)) for name, pattern in PATTERNS)


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def find(value: Any) -> list[str]:
    """引っかかった「項目名: 種類」を返す。値そのものは返さない（ログや応答へ漏らさない）。"""
    hits: list[str] = []
    for path, text in _strings(value):
        for line in text.splitlines() or [text]:
            if ALLOW.search(line):
                continue
            for name, pattern in _COMPILED:
                if pattern.search(line):
                    label = f"{path or 'value'}: {name}"
                    if label not in hits:
                        hits.append(label)
    return hits
