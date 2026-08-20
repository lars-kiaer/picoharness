"""Cost model for the provider selection policy (section 6.4 and 12.4).

A provider manifest declares an estimated cost. That number comes from
`probe()`, which runs once at install. It goes stale the moment the machine
changes, the file cache warms up, or another process takes the CPU.

This module closes the loop. Every call already writes its duration into the
ledger. The ledger is already indexed into `fact` and `failure`. So the
measured cost of a provider is a query, not a new subsystem.

Two rules keep it honest:

* **A small sample does not override the manifest.** Below `min_samples` the
  estimate reports `confidence = "manifest"` and the caller keeps its declared
  number. One slow call is not a trend.
* **Use p90, not the mean.** The selection policy has a budget to respect, so
  it needs a number it will beat most of the time. A mean hides the tail that
  actually breaks the budget. Note the consequence: a provider that is usually
  fast but occasionally very slow loses to a provider that is steadily slower.
  That is intended. A budget is broken by the tail.

  Note also that at `min_samples` the p90 is simply the slowest call observed.
  Read a `low` confidence p90 as an upper bound, not as a typical cost.

The cost model never picks a provider. It answers "how long does this one
take". Section 6.4 does the picking, in deterministic code.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

Confidence = Literal["manifest", "low", "good"]

#: Below this many observations, keep the manifest value.
DEFAULT_MIN_SAMPLES = 5

#: Above this many, treat the measurement as settled.
GOOD_SAMPLES = 20


COST_SCHEMA = """
-- Observed cost per provider and schema, over both successes and failures.
-- A failed call still consumed the time, so it belongs in the estimate.
CREATE VIEW IF NOT EXISTS v_provider_cost AS
WITH call AS (
    SELECT provider_id, schema_id, duration_ms, observed_at, 1 AS ok
    FROM fact    WHERE duration_ms IS NOT NULL AND provider_id IS NOT NULL
    UNION ALL
    SELECT provider_id, schema_id, duration_ms, observed_at, 0 AS ok
    FROM failure WHERE duration_ms IS NOT NULL AND provider_id IS NOT NULL
),
ranked AS (
    SELECT provider_id, schema_id, duration_ms, ok, observed_at,
           ROW_NUMBER() OVER (PARTITION BY provider_id, schema_id
                              ORDER BY duration_ms) AS rn,
           COUNT(*)    OVER (PARTITION BY provider_id, schema_id) AS n
    FROM call
)
SELECT
    provider_id,
    schema_id,
    n                                            AS calls,
    ROUND(AVG(duration_ms), 1)                   AS mean_ms,
    MIN(duration_ms)                             AS min_ms,
    MAX(duration_ms)                             AS max_ms,
    ROUND(MAX(CASE WHEN rn <= (n + 1) / 2         THEN duration_ms END), 1) AS p50_ms,
    ROUND(MAX(CASE WHEN rn <= (n * 9 + 9) / 10    THEN duration_ms END), 1) AS p90_ms,
    ROUND(1.0 * SUM(ok) / n, 3)                  AS ok_rate,
    MAX(observed_at)                             AS last_seen
FROM ranked
GROUP BY provider_id, schema_id;
"""


@dataclass(frozen=True, slots=True)
class Estimate:
    """What one call to this provider is expected to cost."""

    provider_id: str
    schema_id: str | None
    calls: int
    p50_ms: float | None
    p90_ms: float | None
    mean_ms: float | None
    ok_rate: float | None
    confidence: Confidence

    @property
    def budget_ms(self) -> float | None:
        """The number to charge against the budget. None means: use the manifest."""
        return self.p90_ms if self.confidence != "manifest" else None

    def max_observed(self, model: CostModel) -> float | None:
        """The slowest call seen. At small sample sizes this equals `p90_ms`."""
        row = model.conn.execute(
            "SELECT MAX(max_ms) AS m FROM v_provider_cost WHERE provider_id = ?"
            " AND (? IS NULL OR schema_id IS ?)",
            (self.provider_id, self.schema_id, self.schema_id),
        ).fetchone()
        return row["m"] if row else None

    def line(self) -> str:
        if self.confidence == "manifest":
            return f"{self.provider_id}: no measurement yet ({self.calls} calls)"
        return (
            f"{self.provider_id}: p50 {self.p50_ms} ms, p90 {self.p90_ms} ms "
            f"over {self.calls} calls ({self.confidence})"
        )


class CostModel:
    """Query layer over `v_provider_cost`. Populated by the normal ingest."""

    def __init__(self, conn: sqlite3.Connection, *, min_samples: int = DEFAULT_MIN_SAMPLES) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(COST_SCHEMA)
        self.conn.commit()
        self.min_samples = min_samples

    def estimate(self, provider_id: str, schema_id: str | None = None) -> Estimate:
        """Measured cost of one call, or a `manifest` verdict if too few calls.

        With no `schema_id`, the estimate pools every schema this provider
        served. That is the right default when the policy only needs a rough
        ordering between providers.
        """
        if schema_id is None:
            row = self.conn.execute(
                "SELECT provider_id, NULL AS schema_id, SUM(calls) AS calls,"
                " ROUND(AVG(mean_ms),1) AS mean_ms, ROUND(MAX(p50_ms),1) AS p50_ms,"
                " ROUND(MAX(p90_ms),1) AS p90_ms, ROUND(AVG(ok_rate),3) AS ok_rate"
                " FROM v_provider_cost WHERE provider_id = ? GROUP BY provider_id",
                (provider_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT provider_id, schema_id, calls, mean_ms, p50_ms, p90_ms, ok_rate"
                " FROM v_provider_cost WHERE provider_id = ? AND schema_id IS ?",
                (provider_id, schema_id),
            ).fetchone()

        calls = int(row["calls"]) if row and row["calls"] else 0
        if calls < self.min_samples:
            return Estimate(provider_id, schema_id, calls, None, None, None, None, "manifest")
        return Estimate(
            provider_id=provider_id,
            schema_id=schema_id,
            calls=calls,
            p50_ms=row["p50_ms"],
            p90_ms=row["p90_ms"],
            mean_ms=row["mean_ms"],
            ok_rate=row["ok_rate"],
            confidence="good" if calls >= GOOD_SAMPLES else "low",
        )

    def cheapest(self, provider_ids: list[str], schema_id: str | None = None) -> str | None:
        """Order candidates by measured p90. Returns None if none are measured.

        The policy in section 6.4 calls this AFTER its own filters. It is a
        tie-break on cost, not a decision about capability, trust, or quality.
        """
        measured = [
            (e.budget_ms, pid)
            for pid in provider_ids
            if (e := self.estimate(pid, schema_id)).budget_ms is not None
        ]
        return min(measured)[1] if measured else None

    def calibration_report(self) -> list[sqlite3.Row]:
        """Everything measured so far, slowest first. For the operator."""
        return self.conn.execute(
            "SELECT provider_id, schema_id, calls, p50_ms, p90_ms, mean_ms,"
            " ok_rate, last_seen FROM v_provider_cost ORDER BY p90_ms DESC"
        ).fetchall()

    def stale_manifests(self) -> list[sqlite3.Row]:
        """Providers whose measured p90 is far from a typical manifest guess.

        Run this after a hardware change. A manifest written on one machine is
        a guess on the next one, which is the whole point of section 12.4.
        """
        return self.conn.execute(
            "SELECT provider_id, schema_id, calls, p50_ms, p90_ms,"
            " ROUND(p90_ms / NULLIF(p50_ms, 0), 2) AS tail_ratio"
            " FROM v_provider_cost WHERE calls >= ? AND p90_ms > 2 * p50_ms"
            " ORDER BY tail_ratio DESC",
            (self.min_samples,),
        ).fetchall()


__all__ = ["CostModel", "Estimate", "COST_SCHEMA", "DEFAULT_MIN_SAMPLES", "GOOD_SAMPLES"]
