"""What the selection policy knew when it chose. Sections 6.4, 10.5 and 12.4.

Two of the six filters in section 6.4 need numbers no manifest can carry: the
measured pass rate of a provider on a schema, and what one call really costs on
this machine. Both are already in the ledger, so both are a query and not a new
subsystem. Section 10.5 puts it in one line: do not build a second counter.

This module holds the shape of those numbers, and nothing else. The read lives
in `picoharness.memory.policy`, beside the views it reads, for two reasons. The
core must not need the memory layers to be present — a box with no
`episodic.db` still runs, it only routes on manifests. And a measurement is
derived data, which is what the memory layer is for.

**The numbers are frozen for the length of a task.** A provider call writes a
duration, ingest turns it into a measurement, and a policy that re-read the
database between two steps would route the second one differently for a reason
the ledger cannot explain. Section 10.3 asks for deterministic replay, so the
numbers that steered a run are written into it and fed back on replay, in the
way `ReplayClock` feeds back the times.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Measure:
    """What the ledger knows about one provider on one schema.

    `None` in either number means "too few calls to say" and never "zero". The
    distinction is the same one section 6.5 draws about confidence: a missing
    number is not a low number, and reading it as one would demote every
    provider on the day it was installed.
    """

    calls: int = 0
    p90_ms: float | None = None
    pass_rate: float | None = None


#: A provider nobody has run yet. Neither filter in section 6.4 can touch it.
UNMEASURED = Measure()


class Snapshot:
    """Every measurement one task may use. Read once, then read-only."""

    __slots__ = ("rows",)

    def __init__(self, rows: dict[tuple[str, str | None], Measure] | None = None) -> None:
        self.rows = dict(rows or {})

    def __bool__(self) -> bool:
        return bool(self.rows)

    def of(self, provider_id: str, schema: str | None = None) -> Measure:
        """This pair, or the same provider over every schema pooled.

        The fall-back matters on the day a schema is added: the provider is
        known, the pair is not, and the pooled row is a better answer than
        pretending nothing was ever measured.
        """
        row = self.rows.get((provider_id, schema))
        if row is None and schema is not None:
            row = self.rows.get((provider_id, None))
        return row or UNMEASURED

    def to_json(self) -> list[dict[str, Any]]:
        """The form written to the ledger. Sorted, because a diff must be stable."""
        return [
            {
                "provider": provider_id,
                "schema": schema,
                "calls": row.calls,
                "p90_ms": row.p90_ms,
                "pass_rate": row.pass_rate,
            }
            for (provider_id, schema), row in sorted(
                self.rows.items(), key=lambda item: (item[0][0], item[0][1] or "")
            )
        ]

    @classmethod
    def from_json(cls, rows: list[dict[str, Any]]) -> Snapshot:
        """Read a snapshot back out of a ledger, for a replay."""
        return cls(
            {
                (row["provider"], row["schema"]): Measure(
                    calls=row.get("calls", 0),
                    p90_ms=row.get("p90_ms"),
                    pass_rate=row.get("pass_rate"),
                )
                for row in rows
            }
        )


__all__ = ["Measure", "Snapshot", "UNMEASURED"]
