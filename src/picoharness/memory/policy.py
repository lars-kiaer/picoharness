"""The two measured filters of section 6.4, read from the derived views.

Section 6.4 filters candidates on measured pass rate and on estimated cost.
Section 10.5 says not to build a second counter for either one:
`v_provider_health` already computes the pass rate, `v_provider_cost` already
computes the p90, and both are derived from the ledgers, so neither can drift
away from what happened.

The small-sample rule of section 12.4 is applied here and not in the policy.
Below `min_samples` a row reports its call count and no numbers at all, so the
policy sees "too few calls to say" and keeps what the manifest declared. One
slow call is not a trend, and one failure is not a bad provider.
"""

from __future__ import annotations

import sqlite3

from ..policy import Measure, Snapshot
from .cost import DEFAULT_MIN_SAMPLES, CostModel


def read_measurements(
    conn: sqlite3.Connection, *, min_samples: int = DEFAULT_MIN_SAMPLES
) -> Snapshot:
    """One read of both views, for one task. Section 12.4.

    Pass the connection of an `EpisodicIndex`: it owns the `fact` and `failure`
    tables that both views are built from. The result is a plain snapshot with
    no connection in it, because the policy must not be able to re-read the
    database in the middle of a task.
    """
    cost = CostModel(conn, min_samples=min_samples)
    conn.row_factory = sqlite3.Row

    health: dict[tuple[str, str | None], tuple[int, float | None]] = {}
    for row in conn.execute(
        "SELECT provider_id, schema_id, facts_ok + failures AS n, pass_rate"
        " FROM v_provider_health"
    ):
        health[(row["provider_id"], row["schema_id"])] = (row["n"], row["pass_rate"])
    # The pooled row per provider, for a schema nobody has measured yet.
    for row in conn.execute(
        "SELECT provider_id, SUM(facts_ok + failures) AS n,"
        " ROUND(1.0 * SUM(facts_ok) / NULLIF(SUM(facts_ok + failures), 0), 4) AS pass_rate"
        " FROM v_provider_health GROUP BY provider_id"
    ):
        health[(row["provider_id"], None)] = (row["n"], row["pass_rate"])

    pairs = set(health)
    pairs |= {
        (row["provider_id"], row["schema_id"])
        for row in conn.execute("SELECT provider_id, schema_id FROM v_provider_cost")
    }
    pairs |= {(provider_id, None) for provider_id, _ in set(pairs)}

    rows: dict[tuple[str, str | None], Measure] = {}
    for provider_id, schema in pairs:
        estimate = cost.estimate(provider_id, schema)
        calls, pass_rate = health.get((provider_id, schema), (0, None))
        rows[(provider_id, schema)] = Measure(
            calls=max(estimate.calls, calls),
            p90_ms=estimate.budget_ms,
            pass_rate=pass_rate if calls >= min_samples else None,
        )
    return Snapshot(rows)


__all__ = ["read_measurements"]
