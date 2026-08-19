"""Episodic index for the Serial Micro-Agent Harness.

Cross-session memory. It answers "what happened last time".

Design rules, from the whitepaper:

* The index is DERIVED. The session ledgers are the only source of truth.
  You can delete the database file and rebuild it. A test asserts this.
* Retrieval path 1 uses no model: structured lookup plus FTS5 with BM25.
  Paths 2 and 3 (vector, model reduction) attach at `RecallPolicy`.
* Trust is inherited and permanent. A fact derived from T1 data stays T1
  for ever. `recall()` refuses to serve T1 facts to a control capability.
* Every recalled fact carries its provenance: session, ledger path, and the
  sequence number of the event that produced it.

No third-party dependencies. Python 3.11+.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from .cost import COST_SCHEMA
from .failure import FAILURE_SCHEMA, classify_event

Trust = Literal["T0", "T1", "T2"]
BudgetClass = Literal["interactive", "attended", "background", "batch"]

TRUST_ORDER: dict[str, int] = {"T2": 0, "T0": 1, "T1": 2}
CONTROL_SAFE: frozenset[str] = frozenset({"T0", "T2"})


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episode (
    session_id       TEXT PRIMARY KEY,
    ledger_path      TEXT NOT NULL,
    ledger_sha256    TEXT NOT NULL,
    composition_hash TEXT,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    goal             TEXT,
    outcome          TEXT NOT NULL DEFAULT 'unknown',
    event_count      INTEGER NOT NULL DEFAULT 0,
    fail_count       INTEGER NOT NULL DEFAULT 0,
    gap_count        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fact (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES episode(session_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    schema_id   TEXT NOT NULL,
    provider_id TEXT,
    trust       TEXT NOT NULL,
    subject     TEXT,
    payload     TEXT NOT NULL,
    text        TEXT NOT NULL,
    valid_until TEXT,
    duration_ms REAL,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_fact_schema  ON fact (schema_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_fact_subject ON fact (subject, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_fact_time    ON fact (observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_fact_cost    ON fact (provider_id, schema_id, duration_ms);

-- The typed projection. This is what makes a range query need no model.
CREATE TABLE IF NOT EXISTS fact_field (
    fact_id INTEGER NOT NULL REFERENCES fact(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    num     REAL,
    txt     TEXT,
    PRIMARY KEY (fact_id, key)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_field_num ON fact_field (key, num);
CREATE INDEX IF NOT EXISTS ix_field_txt ON fact_field (key, txt);

CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5 (
    text,
    subject,
    content = 'fact',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS episode_fts USING fts5 (
    goal,
    content = 'episode',
    content_rowid = 'rowid',
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


# --------------------------------------------------------------------------
# value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a fact came from. Enough to find the original event."""

    session_id: str
    seq: int
    ledger_path: str
    provider_id: str | None
    observed_at: str

    def cite(self) -> str:
        return f"{self.ledger_path}#{self.seq}"


@dataclass(frozen=True, slots=True)
class Recalled:
    payload: dict[str, Any]
    schema_id: str
    trust: Trust
    subject: str | None
    score: float
    provenance: Provenance
    goal: str | None = None

    def is_control_safe(self) -> bool:
        return self.trust in CONTROL_SAFE


@dataclass(slots=True)
class RecallPolicy:
    """Budget-aware retrieval. Mirrors section 6.4 of the whitepaper.

    Path 1 (structured plus FTS5) always runs and uses no model.
    Path 2 and path 3 attach here. They stay unset until you measure that
    path 1 is not enough.
    """

    budget_class: BudgetClass = "attended"
    limit: int = 8
    max_age_days: int | None = None
    include_expired: bool = False
    allow_trust: frozenset[str] = field(default_factory=lambda: frozenset({"T0", "T1", "T2"}))
    for_control: bool = False

    def effective_limit(self) -> int:
        if self.budget_class == "interactive":
            return min(self.limit, 5)
        return self.limit

    def vector_allowed(self) -> bool:
        return self.budget_class in ("background", "batch")


class Embedder(Protocol):
    """Path 2 plug point. Nothing in this module implements it."""

    id: str

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class TrustViolation(RuntimeError):
    """Raised when untrusted memory would reach the control plane."""


# --------------------------------------------------------------------------
# ledger reading
# --------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def read_events(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the events of one ledger. A bad line stops the read loudly."""
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} is not valid JSON") from exc


def flatten_payload(payload: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Walk a JSON object into (dotted key, scalar) pairs."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield from flatten_payload(value, f"{prefix}{key}.")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from flatten_payload(value, f"{prefix}{index}.")
    else:
        yield prefix.rstrip("."), payload


def searchable_text(payload: Any) -> str:
    """Render a payload for FTS. Keep the key names: users search on them."""
    parts: list[str] = []
    for key, value in flatten_payload(payload):
        if value is None or value == "":
            continue
        parts.append(key.replace(".", " ").replace("_", " "))
        parts.append(str(value))
    return " ".join(parts)


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------


class EpisodicIndex:
    """Cross-session memory over a directory of session ledgers."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.executescript(FAILURE_SCHEMA)
        self.conn.executescript(COST_SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> EpisodicIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- ingest ------------------------------------------------------------

    def ingest_ledger(self, ledger_path: str | Path, *, force: bool = False) -> str | None:
        """Index one `events.jsonl`. Idempotent. Returns the session id.

        The ledger hash decides whether work is needed. A ledger that grew
        since the last run is re-indexed from scratch, which is correct and
        cheap at this scale.
        """
        path = Path(ledger_path)
        digest = _sha256_file(path)

        row = self.conn.execute(
            "SELECT session_id, ledger_sha256 FROM episode WHERE ledger_path = ?",
            (str(path),),
        ).fetchone()
        if row is not None and row["ledger_sha256"] == digest and not force:
            return row["session_id"]
        if row is not None:
            self._delete_session(row["session_id"])

        episode = _EpisodeBuilder(ledger_path=str(path), ledger_sha256=digest)
        for event in read_events(path):
            episode.consume(event)

        if episode.session_id is None:
            return None

        episode.link_resolutions()
        self._write_episode(episode)
        self.conn.commit()
        return episode.session_id

    def ingest_dir(self, sessions_dir: str | Path, pattern: str = "*/events.jsonl") -> list[str]:
        """Index every ledger under a directory. Returns the session ids."""
        found: list[str] = []
        for path in sorted(Path(sessions_dir).glob(pattern)):
            session_id = self.ingest_ledger(path)
            if session_id is not None:
                found.append(session_id)
        return found

    def rebuild(self, sessions_dir: str | Path) -> list[str]:
        """Drop everything and index again. The index is derived, so this
        must give the same result as an incremental ingest."""
        self.conn.executescript(
            "DELETE FROM failure; DELETE FROM fact_field; DELETE FROM fact_fts; DELETE FROM fact;"
            " DELETE FROM episode_fts; DELETE FROM episode;"
        )
        self.conn.commit()
        return self.ingest_dir(sessions_dir)

    def _delete_session(self, session_id: str) -> None:
        ids = [
            r["id"]
            for r in self.conn.execute("SELECT id FROM fact WHERE session_id = ?", (session_id,))
        ]
        for fact_id in ids:
            self.conn.execute("INSERT INTO fact_fts (fact_fts, rowid, text, subject) "
                              "SELECT 'delete', id, text, subject FROM fact WHERE id = ?", (fact_id,))
        erow = self.conn.execute(
            "SELECT rowid, goal FROM episode WHERE session_id = ?", (session_id,)
        ).fetchone()
        if erow is not None:
            self.conn.execute(
                "INSERT INTO episode_fts (episode_fts, rowid, goal) VALUES ('delete', ?, ?)",
                (erow["rowid"], erow["goal"]),
            )
        self.conn.execute("DELETE FROM failure WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM fact WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM episode WHERE session_id = ?", (session_id,))

    def _write_episode(self, ep: _EpisodeBuilder) -> None:
        cur = self.conn.execute(
            "INSERT INTO episode (session_id, ledger_path, ledger_sha256, composition_hash,"
            " started_at, ended_at, goal, outcome, event_count, fail_count, gap_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                ep.session_id, ep.ledger_path, ep.ledger_sha256, ep.composition_hash,
                ep.started_at, ep.ended_at, ep.goal, ep.outcome,
                ep.event_count, ep.fail_count, ep.gap_count,
            ),
        )
        self.conn.execute(
            "INSERT INTO episode_fts (rowid, goal) VALUES (?, ?)",
            (cur.lastrowid, ep.goal or ""),
        )

        for fact in ep.facts:
            text = searchable_text(fact["payload"])
            cur = self.conn.execute(
                "INSERT INTO fact (session_id, seq, observed_at, schema_id, provider_id,"
                " trust, subject, payload, text, valid_until, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ep.session_id, fact["seq"], fact["observed_at"], fact["schema_id"],
                    fact["provider_id"], fact["trust"], fact["subject"],
                    json.dumps(fact["payload"], ensure_ascii=False, sort_keys=True),
                    text, fact["valid_until"], fact["duration_ms"],
                ),
            )
            fact_id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO fact_fts (rowid, text, subject) VALUES (?,?,?)",
                (fact_id, text, fact["subject"] or ""),
            )
            rows = []
            for key, value in flatten_payload(fact["payload"]):
                if isinstance(value, bool):
                    rows.append((fact_id, key, float(value), str(value).lower()))
                elif isinstance(value, (int, float)):
                    rows.append((fact_id, key, float(value), None))
                elif value is None:
                    continue
                else:
                    rows.append((fact_id, key, None, str(value)))
            self.conn.executemany(
                "INSERT OR REPLACE INTO fact_field (fact_id, key, num, txt) VALUES (?,?,?,?)",
                rows,
            )

        self.conn.executemany(
            "INSERT INTO failure (session_id, seq, observed_at, kind, step, capability,"
            " tool, provider_id, schema_id, signature, detail, detail_trust, attempt,"
            " resolution, resolved_by, resolved_seq, duration_ms)"
            " VALUES (:session_id,:seq,:observed_at,:kind,:step,:capability,:tool,"
            ":provider_id,:schema_id,:signature,:detail,:detail_trust,:attempt,"
            ":resolution,:resolved_by,:resolved_seq,:duration_ms)",
            [
                {
                    "session_id": ep.session_id,
                    "resolution": None, "resolved_by": None, "resolved_seq": None,
                    **f,
                }
                for f in ep.failures
            ],
        )

    # -- retrieval, path 1 -------------------------------------------------

    def recall(
        self,
        query: str = "",
        *,
        policy: RecallPolicy | None = None,
        schema_id: str | None = None,
        subject: str | None = None,
        since: str | None = None,
    ) -> list[Recalled]:
        """Path 1 retrieval. No model call. BM25 ranking from FTS5.

        An empty query with a filter is a plain structured lookup, which is
        the case that most real questions reduce to.
        """
        policy = policy or RecallPolicy()
        allowed = CONTROL_SAFE & policy.allow_trust if policy.for_control else policy.allow_trust
        if not allowed:
            return []

        ranked = bool(query.strip())
        rank_col = "bm25(fact_fts) AS bm25" if ranked else "0.0 AS bm25"
        sql = [
            f"SELECT f.*, e.goal AS goal, e.ledger_path AS ledger_path, {rank_col}",
            "FROM fact f JOIN episode e ON e.session_id = f.session_id",
        ]
        params: list[Any] = []
        where = ["f.trust IN (%s)" % ",".join("?" * len(allowed))]
        params.extend(sorted(allowed))

        if ranked:
            sql.append("JOIN fact_fts ON fact_fts.rowid = f.id")
            where.append("fact_fts MATCH ?")
            params.append(_fts_query(query))
        if schema_id:
            where.append("f.schema_id = ?")
            params.append(schema_id)
        if subject:
            where.append("f.subject = ?")
            params.append(subject)
        if since:
            where.append("f.observed_at >= ?")
            params.append(since)
        if policy.max_age_days is not None:
            where.append("f.observed_at >= ?")
            params.append(_days_ago(policy.max_age_days))
        if not policy.include_expired:
            where.append("(f.valid_until IS NULL OR f.valid_until >= ?)")
            params.append(_now())

        sql.append("WHERE " + " AND ".join(where))
        order = "ORDER BY bm25 ASC, f.observed_at DESC" if ranked else "ORDER BY f.observed_at DESC"
        sql.append(order)
        sql.append("LIMIT ?")
        params.append(policy.effective_limit())

        rows = self.conn.execute("\n".join(sql), params).fetchall()
        return [self._to_recalled(r, ranked=ranked) for r in rows]

    def recall_for_control(self, query: str = "", **kwargs: Any) -> list[Recalled]:
        """Retrieval for a `plan@1` or `route@1` provider.

        Section 11.2 says a provider that consumed T1 must not emit control.
        Memory is where that rule is easiest to break: a poisoned log line
        becomes a fact, and three weeks later the fact looks like history.
        Trust is inherited, so the filter still holds.
        """
        policy: RecallPolicy = kwargs.pop("policy", None) or RecallPolicy()
        policy.for_control = True
        results = self.recall(query, policy=policy, **kwargs)
        leaked = [r for r in results if not r.is_control_safe()]
        if leaked:
            raise TrustViolation(
                f"{len(leaked)} untrusted fact(s) reached a control query; "
                f"first: {leaked[0].provenance.cite()}"
            )
        return results

    def field_range(
        self,
        key: str,
        *,
        below: float | None = None,
        above: float | None = None,
        policy: RecallPolicy | None = None,
    ) -> list[Recalled]:
        """A numeric question, answered by SQL. Zero model calls.

        Example: `field_range("disk_free_pct", below=15)`.
        """
        policy = policy or RecallPolicy()
        allowed = CONTROL_SAFE & policy.allow_trust if policy.for_control else policy.allow_trust
        where = ["ff.key = ?", "ff.num IS NOT NULL",
                 "f.trust IN (%s)" % ",".join("?" * len(allowed))]
        params: list[Any] = [key, *sorted(allowed)]
        if below is not None:
            where.append("ff.num < ?")
            params.append(below)
        if above is not None:
            where.append("ff.num > ?")
            params.append(above)
        if not policy.include_expired:
            where.append("(f.valid_until IS NULL OR f.valid_until >= ?)")
            params.append(_now())
        params.append(policy.effective_limit())

        rows = self.conn.execute(
            "SELECT f.*, e.goal AS goal, e.ledger_path AS ledger_path, ff.num AS bm25 "
            "FROM fact_field ff JOIN fact f ON f.id = ff.fact_id "
            "JOIN episode e ON e.session_id = f.session_id "
            f"WHERE {' AND '.join(where)} ORDER BY f.observed_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._to_recalled(r, ranked=False) for r in rows]

    def similar_episodes(self, goal: str, limit: int = 5) -> list[sqlite3.Row]:
        """"Have I done this before?" Path 1 over episode goals."""
        if not goal.strip():
            return []
        return self.conn.execute(
            "SELECT e.*, bm25(episode_fts) AS score FROM episode_fts "
            "JOIN episode e ON e.rowid = episode_fts.rowid "
            "WHERE episode_fts MATCH ? ORDER BY score ASC LIMIT ?",
            (_fts_query(goal), limit),
        ).fetchall()

    def stats(self) -> dict[str, int]:
        one = lambda q: self.conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "episodes": one("SELECT COUNT(*) FROM episode"),
            "facts": one("SELECT COUNT(*) FROM fact"),
            "fields": one("SELECT COUNT(*) FROM fact_field"),
            "untrusted_facts": one("SELECT COUNT(*) FROM fact WHERE trust = 'T1'"),
            "failures": one("SELECT COUNT(*) FROM failure"),
            "unresolved": one("SELECT COUNT(*) FROM failure WHERE resolution IN ('abandoned','declined')"),
        }

    def _to_recalled(self, row: sqlite3.Row, *, ranked: bool) -> Recalled:
        return Recalled(
            payload=json.loads(row["payload"]),
            schema_id=row["schema_id"],
            trust=row["trust"],
            subject=row["subject"],
            score=float(row["bm25"]) if ranked else 0.0,
            goal=row["goal"],
            provenance=Provenance(
                session_id=row["session_id"],
                seq=row["seq"],
                ledger_path=row["ledger_path"],
                provider_id=row["provider_id"],
                observed_at=row["observed_at"],
            ),
        )


# --------------------------------------------------------------------------
# ledger -> episode
# --------------------------------------------------------------------------


@dataclass
class _EpisodeBuilder:
    ledger_path: str
    ledger_sha256: str
    session_id: str | None = None
    composition_hash: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    goal: str | None = None
    outcome: str = "unknown"
    event_count: int = 0
    fail_count: int = 0
    gap_count: int = 0
    facts: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    replanned_after: list[int] = field(default_factory=list)
    _step_trust: dict[str, str] = field(default_factory=dict)
    _step_subject: dict[str, str] = field(default_factory=dict)

    def consume(self, event: dict[str, Any]) -> None:
        self.event_count += 1
        kind = event.get("type")
        when = event.get("t")
        if when and (self.started_at is None or when < self.started_at):
            self.started_at = when
        if when and (self.ended_at is None or when > self.ended_at):
            self.ended_at = when

        if kind == "composition":
            self.composition_hash = event.get("hash")
            self.session_id = self.session_id or event.get("session_id")
        elif kind == "user_input":
            self.goal = self.goal or event.get("text")
            self.session_id = self.session_id or event.get("session_id")
        elif kind == "step_started":
            # The tool declares the trust of its output. The fact inherits it.
            self._step_trust[event["step"]] = event.get("trust", "T1")
            if event.get("subject"):
                self._step_subject[event["step"]] = event["subject"]
        elif kind == "fact_added":
            step = event.get("step", "")
            self.facts.append(
                {
                    "seq": event["seq"],
                    "step": step,
                    "observed_at": when or "",
                    "schema_id": event.get("schema", "unknown@0"),
                    "provider_id": event.get("provider"),
                    # Inherited, never upgraded. This is the whole point.
                    "trust": event.get("trust") or self._step_trust.get(step, "T1"),
                    "subject": event.get("subject") or self._step_subject.get(step),
                    "payload": event.get("fact", {}),
                    "valid_until": event.get("valid_until"),
                    "duration_ms": event.get("duration_ms"),
                }
            )
        elif kind == "answer_sent":
            self.outcome = event.get("outcome", "answered")
        elif kind == "declined":
            self.outcome = "declined"
        elif kind == "plan_created" and self.facts:
            self.replanned_after.append(event["seq"])

        failure = classify_event(event)
        if failure is not None:
            if failure["kind"] == "capability_gap":
                self.gap_count += 1
            else:
                self.fail_count += 1
            self.failures.append(failure)

        if self.session_id is None:
            self.session_id = event.get("session_id")

    def link_resolutions(self) -> None:
        """Say what happened after each failure.

        A failure with no remedy is a warning. A failure with a remedy is a
        routing hint, and that is what makes this table useful to a planner.
        """
        by_step: dict[str, list[dict[str, Any]]] = {}
        for fact in self.facts:
            by_step.setdefault(fact.get("step") or "", []).append(fact)

        for fail in self.failures:
            step = fail.get("step") or ""
            later = [f for f in by_step.get(step, []) if f["seq"] > fail["seq"]]
            if later:
                fix = min(later, key=lambda f: f["seq"])
                same = fix["provider_id"] == fail.get("provider_id")
                fail["resolution"] = "retry_same" if same else "escalated"
                fail["resolved_by"] = fix["provider_id"]
                fail["resolved_seq"] = fix["seq"]
            elif self.replanned_after and any(
                seq > fail["seq"] for seq in self.replanned_after
            ):
                fail["resolution"] = "replanned"
            elif self.outcome == "declined":
                fail["resolution"] = "declined"
            else:
                fail["resolution"] = "abandoned"

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = ""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago(days: int) -> str:
    ts = datetime.now(timezone.utc).timestamp() - days * 86400
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fts_query(raw: str) -> str:
    """Make a safe FTS5 query. Quote every token, then OR them.

    A user question is not FTS5 syntax. An unquoted token can be an operator
    and cause a syntax error, or worse, a silent change of meaning.
    """
    tokens = [t for t in "".join(c if c.isalnum() or c in "_-" else " " for c in raw).split() if len(t) > 1]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


__all__ = [
    "EpisodicIndex",
    "RecallPolicy",
    "Recalled",
    "Provenance",
    "TrustViolation",
    "Embedder",
]
