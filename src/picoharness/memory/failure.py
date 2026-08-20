"""Failure memory for the Serial Micro-Agent Harness.

Two consumers, one table.

* The **planner** asks "what went wrong last time I tried this" before it
  commits to a step. It gets structured records only, never free text.
  See `FailureMemory.avoidance()`.
* **You** ask "what is going wrong in my system". That is SQL. See the views
  in `FAILURE_SCHEMA` and the named queries in `REPORT_SQL`, or run
  `python3 failure_memory.py report --db memory/episodic.db`.

Why the split matters. A failure detail can carry text that came from a tool,
and a tool reads untrusted data. "Column 'x' not found" is safe. An error that
echoes a log line is not. So the detail column keeps its own trust level, and
the planner path never reads it. You do, because you are a person looking at a
report, not a control plane.

The store lives in the same SQLite file as the episodic index. One ingest pass
fills both.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

FailureKind = Literal[
    "grammar",          # output could not be parsed at all
    "schema",           # failed the schema check
    "range",            # a value was out of range, or the unit was wrong
    "crosscheck",       # deterministic cross-check disagreed (ladder level 4)
    "semantic",         # a critic rejected the meaning
    "tool_error",       # the tool itself failed
    "tool_timeout",
    "empty",            # the tool returned nothing to work with
    "capability_gap",   # no provider could serve the capability
    "budget",           # a breaker or the budget stopped the work
    "approval_denied",
    "unknown",
]

Resolution = Literal[
    "retry_same",       # the same provider succeeded on a later attempt
    "escalated",        # a larger provider succeeded
    "replanned",        # the planner produced a different route
    "declined",         # the system told the user it could not do it
    "abandoned",        # the session ended with the step still failed
]

#: Kinds whose detail text can contain data from outside the system.
TAINTED_KINDS: frozenset[str] = frozenset(
    {"grammar", "schema", "range", "crosscheck", "semantic", "tool_error", "empty"}
)

EVENT_TO_KIND: dict[str, str] = {
    "validation_failed": "schema",
    "grammar_failed": "grammar",
    "range_failed": "range",
    "crosscheck_failed": "crosscheck",
    "critic_rejected": "semantic",
    "tool_failed": "tool_error",
    "tool_timeout": "tool_timeout",
    "tool_empty": "empty",
    "capability_gap": "capability_gap",
    "breaker_tripped": "budget",
    "budget_exhausted": "budget",
    "approval_denied": "approval_denied",
    "step_failed": "unknown",
}

FAILURE_EVENT_TYPES: frozenset[str] = frozenset(EVENT_TO_KIND)


# --------------------------------------------------------------------------
# schema and views
# --------------------------------------------------------------------------

FAILURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS failure (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES episode(session_id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    observed_at  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    step         TEXT,
    capability   TEXT,
    tool         TEXT,
    provider_id  TEXT,
    schema_id    TEXT,
    signature    TEXT NOT NULL,
    detail       TEXT,
    detail_trust TEXT NOT NULL DEFAULT 'T1',
    attempt      INTEGER NOT NULL DEFAULT 1,
    resolution   TEXT,
    resolved_by  TEXT,
    resolved_seq INTEGER,
    duration_ms  REAL,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_fail_sig  ON failure (signature, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_fail_cap  ON failure (capability, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_fail_tool ON failure (tool, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_fail_prov ON failure (provider_id, schema_id);
CREATE INDEX IF NOT EXISTS ix_fail_kind ON failure (kind, observed_at DESC);

-- One row per distinct failure shape. This is the planner's lookup table.
CREATE VIEW IF NOT EXISTS v_signature AS
SELECT
    signature,
    kind,
    capability,
    tool,
    provider_id,
    schema_id,
    COUNT(*)                                        AS seen,
    MIN(observed_at)                                AS first_seen,
    MAX(observed_at)                                AS last_seen,
    SUM(resolution IS NOT NULL)                     AS resolved,
    SUM(resolution IN ('retry_same','escalated','replanned')) AS recovered
FROM failure
GROUP BY signature;

-- What went wrong per provider and schema. Feeds the demotion rule of 10.5.
CREATE VIEW IF NOT EXISTS v_provider_health AS
WITH pair AS (
    SELECT provider_id, schema_id FROM fact    WHERE provider_id IS NOT NULL
    UNION
    SELECT provider_id, schema_id FROM failure WHERE provider_id IS NOT NULL
)
SELECT
    pair.provider_id,
    pair.schema_id,
    COALESCE(ok.n,  0) AS facts_ok,
    COALESCE(bad.n, 0) AS failures,
    ROUND(CAST(COALESCE(ok.n, 0) AS REAL)
          / NULLIF(COALESCE(ok.n, 0) + COALESCE(bad.n, 0), 0), 4) AS pass_rate
FROM pair
LEFT JOIN (SELECT provider_id, schema_id, COUNT(*) AS n FROM fact
           GROUP BY provider_id, schema_id) ok
  ON ok.provider_id = pair.provider_id AND ok.schema_id IS pair.schema_id
LEFT JOIN (SELECT provider_id, schema_id, COUNT(*) AS n FROM failure
           WHERE provider_id IS NOT NULL GROUP BY provider_id, schema_id) bad
  ON bad.provider_id = pair.provider_id AND bad.schema_id IS pair.schema_id;

-- Failures that never got a remedy. This is the real bug list.
CREATE VIEW IF NOT EXISTS v_unresolved AS
SELECT signature, kind, capability, tool, provider_id, schema_id,
       COUNT(*) AS seen, MAX(observed_at) AS last_seen
FROM failure
WHERE resolution IS NULL OR resolution IN ('declined', 'abandoned')
GROUP BY signature
ORDER BY seen DESC;

-- Which capability to install a provider for next.
CREATE VIEW IF NOT EXISTS v_capability_gap AS
SELECT capability, COUNT(*) AS seen, MAX(observed_at) AS last_seen,
       COUNT(DISTINCT session_id) AS sessions
FROM failure
WHERE kind = 'capability_gap'
GROUP BY capability
ORDER BY seen DESC;
"""


#: Named queries for the operator. Run them here, in the `sqlite3` shell, or
#: from R through `RSQLite` — the file is plain SQLite with no extensions.
REPORT_SQL: dict[str, tuple[str, str]] = {
    "kinds": (
        "Failures by kind, newest window first",
        """
        SELECT kind, COUNT(*) AS seen, COUNT(DISTINCT session_id) AS sessions,
               MAX(observed_at) AS last_seen
        FROM failure
        WHERE observed_at >= :since
        GROUP BY kind
        ORDER BY seen DESC;
        """,
    ),
    "top_signatures": (
        "The failure shapes that cost you the most",
        """
        SELECT seen, kind, COALESCE(provider_id, tool, capability) AS at,
               schema_id, recovered, last_seen, signature
        FROM v_signature
        WHERE last_seen >= :since
        ORDER BY seen DESC
        LIMIT :limit;
        """,
    ),
    "provider_health": (
        "Pass rate per provider and schema; demote below the floor",
        """
        SELECT provider_id, schema_id, facts_ok, failures, pass_rate
        FROM v_provider_health
        WHERE facts_ok + failures >= :min_n
        ORDER BY pass_rate ASC, failures DESC;
        """,
    ),
    "demotion_candidates": (
        "Providers that section 10.5 says to route away from",
        """
        SELECT provider_id, schema_id, facts_ok, failures, pass_rate
        FROM v_provider_health
        WHERE pass_rate < :floor AND facts_ok + failures >= :min_n
        ORDER BY pass_rate ASC;
        """,
    ),
    "unresolved": (
        "Failures with no known remedy",
        "SELECT * FROM v_unresolved WHERE last_seen >= :since LIMIT :limit;",
    ),
    "gaps": (
        "Capabilities with no provider — the shopping list",
        "SELECT * FROM v_capability_gap WHERE last_seen >= :since;",
    ),
    "weekly": (
        "Failure count per week, to see whether it is getting better",
        """
        SELECT substr(observed_at, 1, 4) || '-W' ||
               printf('%02d', CAST(strftime('%W', substr(observed_at,1,10)) AS INTEGER))
                   AS week,
               COUNT(*) AS failures,
               COUNT(DISTINCT session_id) AS sessions,
               ROUND(AVG(attempt), 2) AS mean_attempts
        FROM failure
        WHERE observed_at >= :since
        GROUP BY week
        ORDER BY week;
        """,
    ),
    "recovery": (
        "What actually worked after a failure",
        """
        SELECT COALESCE(resolution, 'none') AS resolution, COUNT(*) AS seen,
               ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM failure
                                         WHERE observed_at >= :since), 1) AS pct
        FROM failure
        WHERE observed_at >= :since
        GROUP BY resolution
        ORDER BY seen DESC;
        """,
    ),
}


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

_NUM = re.compile(r"\d+")
_QUOTED = re.compile(r"""(['"`])(?:(?!\1).)*\1""")
_PATH = re.compile(r"(/[\w.\-]+){2,}")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_WS = re.compile(r"\s+")


def normalise_detail(detail: str | None, *, keep: int = 120) -> str:
    """Reduce an error string to its shape, so that variants group together.

    "column 'user_id' not found"  and  "column 'host' not found"
    both become                   "column <q> not found".

    This also removes most of the tainted content, which is a useful side
    effect but not a defence. The defence is `detail_trust`.
    """
    if not detail:
        return ""
    text = _PATH.sub("<path>", detail)
    text = _HEX.sub("<hex>", text)
    text = _QUOTED.sub("<q>", text)
    text = _NUM.sub("#", text)
    return _WS.sub(" ", text).strip()[:keep]


def signature_for(
    kind: str,
    *,
    capability: str | None = None,
    tool: str | None = None,
    provider_id: str | None = None,
    schema_id: str | None = None,
    detail: str | None = None,
) -> str:
    """A stable grouping key. It never contains raw detail text."""
    parts = [kind, capability or "", tool or "", provider_id or "",
             schema_id or "", normalise_detail(detail)]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def classify_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a ledger event into a failure row, or return None."""
    etype = event.get("type")
    if etype not in EVENT_TO_KIND:
        return None
    kind = event.get("kind") or EVENT_TO_KIND[etype]
    detail = event.get("error") or event.get("reason") or event.get("detail")
    default_trust = "T1" if kind in TAINTED_KINDS else "T2"
    return {
        "seq": event["seq"],
        "observed_at": event.get("t", ""),
        "kind": kind,
        "step": event.get("step"),
        "capability": event.get("capability"),
        "tool": event.get("tool"),
        "provider_id": event.get("provider"),
        "schema_id": event.get("schema"),
        "detail": detail,
        "detail_trust": event.get("detail_trust", default_trust),
        "attempt": int(event.get("attempt", 1)),
        "duration_ms": event.get("duration_ms"),
        "signature": signature_for(
            kind,
            capability=event.get("capability"),
            tool=event.get("tool"),
            provider_id=event.get("provider"),
            schema_id=event.get("schema"),
            detail=detail,
        ),
    }


# --------------------------------------------------------------------------
# the planner path
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Avoidance:
    """One thing that went wrong before, and what worked instead.

    There is no free text here. The planner never sees a detail string,
    because a detail string can carry content from a tool.
    """

    signature: str
    kind: str
    capability: str | None
    tool: str | None
    provider_id: str | None
    schema_id: str | None
    seen: int
    last_seen: str
    remedy: str | None
    remedy_target: str | None
    recovered: int

    @property
    def confidence(self) -> float:
        """How often this failure had a known remedy."""
        return round(self.recovered / self.seen, 2) if self.seen else 0.0

    def line(self) -> str:
        """One short line for the planner prompt. Keep it dense."""
        at = self.provider_id or self.tool or self.capability or "?"
        head = f"{self.kind} at {at} x{self.seen}"
        if self.remedy:
            target = f" -> {self.remedy_target}" if self.remedy_target else ""
            return f"{head}; worked: {self.remedy}{target}"
        return f"{head}; no known remedy"


class FailureMemory:
    """Query layer over the `failure` table. Ingest happens in the index."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(FAILURE_SCHEMA)
        self.conn.commit()

    # -- planner -----------------------------------------------------------

    def avoidance(
        self,
        *,
        capability: str | None = None,
        tool: str | None = None,
        provider_id: str | None = None,
        schema_id: str | None = None,
        min_seen: int = 1,
        limit: int = 5,
    ) -> list[Avoidance]:
        """What went wrong before, for this capability, tool, or provider.

        Path 1 only: indexed lookup, no model, no embedding. Give at least
        one filter, or you get the loudest failures overall.
        """
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("capability", capability),
            ("tool", tool),
            ("provider_id", provider_id),
            ("schema_id", schema_id),
        ):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        clause = ("WHERE " + " OR ".join(where)) if where else ""

        rows = self.conn.execute(
            f"""
            SELECT signature, kind, capability, tool, provider_id, schema_id,
                   COUNT(*) AS seen, MAX(observed_at) AS last_seen,
                   SUM(resolution IN ('retry_same','escalated','replanned')) AS recovered
            FROM failure
            {clause}
            GROUP BY signature
            HAVING seen >= ?
            ORDER BY seen DESC, last_seen DESC
            LIMIT ?
            """,
            [*params, min_seen, limit],
        ).fetchall()

        out: list[Avoidance] = []
        for r in rows:
            remedy, target = self._best_remedy(r["signature"])
            out.append(
                Avoidance(
                    signature=r["signature"], kind=r["kind"],
                    capability=r["capability"], tool=r["tool"],
                    provider_id=r["provider_id"], schema_id=r["schema_id"],
                    seen=r["seen"], last_seen=r["last_seen"],
                    remedy=remedy, remedy_target=target,
                    recovered=r["recovered"] or 0,
                )
            )
        return out

    def _best_remedy(self, signature: str) -> tuple[str | None, str | None]:
        row = self.conn.execute(
            "SELECT resolution, resolved_by, COUNT(*) AS n FROM failure "
            "WHERE signature = ? AND resolution IN ('retry_same','escalated','replanned') "
            "GROUP BY resolution, resolved_by ORDER BY n DESC LIMIT 1",
            (signature,),
        ).fetchone()
        return (row["resolution"], row["resolved_by"]) if row else (None, None)

    def prompt_block(self, items: Sequence[Avoidance], *, max_lines: int = 4) -> str:
        """Render avoidance notes for a planner prompt.

        Small on purpose. A planner that reads twenty past failures has the
        context problem this whole design exists to avoid.
        """
        if not items:
            return ""
        lines = [a.line() for a in items[:max_lines]]
        return "Known failures to avoid:\n" + "\n".join(f"- {ln}" for ln in lines)

    # -- operator ----------------------------------------------------------

    def report(
        self,
        name: str,
        *,
        since: str = "0000",
        limit: int = 15,
        floor: float = 0.8,
        min_n: int = 5,
    ) -> list[sqlite3.Row]:
        """Run one of the named queries in `REPORT_SQL`."""
        if name not in REPORT_SQL:
            raise KeyError(f"unknown report {name!r}; have {sorted(REPORT_SQL)}")
        _, sql = REPORT_SQL[name]
        return self.conn.execute(
            sql, {"since": since, "limit": limit, "floor": floor, "min_n": min_n}
        ).fetchall()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_rows(rows: Sequence[sqlite3.Row]) -> None:
    if not rows:
        print("  (no rows)")
        return
    cols = rows[0].keys()
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths, strict=True)))


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point. A broken pipe is not an error: `... | head`."""
    try:
        return _run(argv)
    except BrokenPipeError:
        return 0


def _run(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Failure memory reports.")
    ap.add_argument("command", choices=["report", "sql", "list"])
    ap.add_argument("--db", default="memory/episodic.db")
    ap.add_argument("--name", default=None, help="one report name; default is all")
    ap.add_argument("--since", default="0000", help="ISO date lower bound")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--floor", type=float, default=0.8)
    ap.add_argument("--min-n", type=int, default=5)
    args = ap.parse_args(argv)

    if args.command == "list":
        for name, (doc, _) in REPORT_SQL.items():
            print(f"{name:20} {doc}")
        return 0

    if args.command == "sql":
        for name, (doc, sql) in REPORT_SQL.items():
            if args.name and name != args.name:
                continue
            print(f"-- {name}: {doc}")
            print(sql.strip(), "\n")
        return 0

    conn = sqlite3.connect(args.db)
    fm = FailureMemory(conn)
    names = [args.name] if args.name else list(REPORT_SQL)
    for name in names:
        doc, _ = REPORT_SQL[name]
        print(f"\n== {name}: {doc}")
        _print_rows(
            fm.report(name, since=args.since, limit=args.limit,
                      floor=args.floor, min_n=args.min_n)
        )
    conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "FailureMemory",
    "Avoidance",
    "FAILURE_SCHEMA",
    "FAILURE_EVENT_TYPES",
    "REPORT_SQL",
    "classify_event",
    "signature_for",
    "normalise_detail",
]
