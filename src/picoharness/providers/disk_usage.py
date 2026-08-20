"""A `code` provider for `extract@1`, producing `disk_usage@1`.

A table sum is SQL and a disk reading is a parser. This is principle P3 at its
least ambiguous: `df` writes fixed columns, so a model here would add latency
and a failure mode in exchange for nothing.
"""

from __future__ import annotations

import re
from typing import Any

from ..payload import Payload

_ROW = re.compile(
    r"^(?P<mount>\S+)\s+\d+\s+\d+\s+\d+\s+(?P<used>\d{1,3})%\s+(?P<on>\S+)\s*$"
)


def extract(payload: Payload) -> dict[str, Any]:
    """Summarise `df -k` output for the fullest file system in it.

    The fullest, and not the first: a machine with six mounts has one that
    matters, and it is the one closest to full. Reporting the first would be a
    silent choice dressed as a fact.
    """
    worst: dict[str, Any] | None = None
    for line in payload.as_text().splitlines():
        row = _ROW.match(line.strip())
        if not row:
            continue
        used = int(row.group("used"))
        if worst is None or used > 100 - worst["disk_free_pct"]:
            worst = {
                "mount": row.group("mount"),
                "mounted_on": row.group("on"),
                "disk_free_pct": 100 - used,
                "state": _state(100 - used),
            }
    return worst or {"mount": None, "mounted_on": None, "disk_free_pct": None, "state": None}


def _state(free_pct: int) -> str:
    if free_pct < 10:
        return "critical"
    if free_pct < 25:
        return "low"
    return "ok"


__all__ = ["extract"]
