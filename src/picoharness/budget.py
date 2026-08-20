"""The task budget and the circuit breakers, section 5.3.

The correct time limit depends on the task, so there is no global one. A budget
is attached when the task starts, the runtime spends it, and it records what it
spent.

The budget does three jobs, and only the first is obvious:

1. It stops a task that is taking too long.
2. **It is given to the planner.** A small budget must produce a short plan.
3. **It filters the provider policy.** When the budget falls, section 6.4
   selects a cheaper provider. This is why the machine profile belongs in the
   composition hash: a slow box spends faster, so it routes differently.

At 80 % spent the runtime stops planning and answers with the facts it holds. A
partial answer with a named gap is better than a timeout.

Breakers are separate, because a budget alone cannot stop a loop. A plan that
alternates between two steps forever can do so inside its wall clock. When a
breaker opens the system must still answer: send the facts collected, and name
what is missing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Literal

BudgetClass = Literal["interactive", "attended", "background", "batch"]


@dataclass(frozen=True, slots=True)
class Limit:
    """What a task may spend. `None` means no limit on that axis."""

    wall_ms: int | None = None
    model_calls: int | None = None
    cpu_ms: int | None = None


#: Section 5.3. The trigger, not the work, decides the class: the user waiting
#: at a screen is a different constraint from a timer at three in the morning.
CLASS_LIMITS: dict[str, Limit] = {
    "interactive": Limit(wall_ms=5_000, model_calls=3, cpu_ms=10_000),
    "attended": Limit(wall_ms=45_000, model_calls=10, cpu_ms=120_000),
    "background": Limit(wall_ms=1_800_000, model_calls=100, cpu_ms=3_600_000),
    "batch": Limit(wall_ms=None, model_calls=None, cpu_ms=None),
}


@dataclass(slots=True)
class Spent:
    wall_ms: float = 0.0
    model_calls: int = 0
    cpu_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class Breakers:
    """Hard stops. A budget cannot catch a loop that is fast."""

    max_steps: int = 8
    max_retries_per_step: int = 2
    max_bytes_to_model: int = 8192


class Budget:
    """One task's allowance. Mutable, because spending is the point."""

    __slots__ = ("_clock", "_started", "breakers", "budget_class", "limit", "spent")

    def __init__(
        self,
        budget_class: BudgetClass = "attended",
        *,
        limit: Limit | None = None,
        breakers: Breakers | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.budget_class = budget_class
        self.limit = limit or CLASS_LIMITS[budget_class]
        self.spent = Spent()
        self.breakers = breakers or Breakers()
        self._clock = clock
        self._started = clock()

    # -- spending ----------------------------------------------------------

    def charge(self, *, wall_ms: float = 0.0, model_calls: int = 0, cpu_ms: float = 0.0) -> None:
        """Record a cost. A failed call still cost the time, so charge it too."""
        self.spent.wall_ms += wall_ms
        self.spent.model_calls += model_calls
        self.spent.cpu_ms += cpu_ms

    def elapsed_ms(self) -> float:
        return (self._clock() - self._started) * 1000.0

    # -- reading -----------------------------------------------------------

    def remaining(self) -> Limit:
        """What is left. Used by the provider policy of section 6.4.

        Wall clock comes from the clock, not from what was charged: time passes
        while the system waits for a tool, and that time is gone whether or not
        anyone recorded it.
        """
        return Limit(
            wall_ms=None
            if self.limit.wall_ms is None
            else max(0, int(self.limit.wall_ms - self.elapsed_ms())),
            model_calls=None
            if self.limit.model_calls is None
            else max(0, self.limit.model_calls - self.spent.model_calls),
            cpu_ms=None
            if self.limit.cpu_ms is None
            else max(0, int(self.limit.cpu_ms - self.spent.cpu_ms)),
        )

    def fraction_spent(self) -> float:
        """The tightest axis, as a fraction. An unlimited axis does not count.

        `None` means unlimited and is skipped. Zero means nothing is allowed on
        that axis, which is spent from the start — a distinction worth keeping,
        because a budget of zero is a legitimate way to say "answer with what
        you already know".
        """
        fractions = []
        for spent, limit in (
            (self.elapsed_ms(), self.limit.wall_ms),
            (self.spent.model_calls, self.limit.model_calls),
            (self.spent.cpu_ms, self.limit.cpu_ms),
        ):
            if limit is None:
                continue
            fractions.append(float("inf") if limit == 0 else spent / limit)
        return max(fractions, default=0.0)

    def exceeded(self) -> bool:
        return self.fraction_spent() >= 1.0

    def should_wind_down(self, at: float = 0.8) -> bool:
        """Stop planning and answer with what is held. Section 5.3 rule 3."""
        return self.fraction_spent() >= at

    def allows_vector_search(self) -> bool:
        """Section 9.3: paths 2 and 3 of the cascade open at `background`."""
        return self.budget_class in ("background", "batch")

    # -- recording ---------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """The shape section 5.3 shows, for the ledger."""
        return {
            "class": self.budget_class,
            "limit": {k: v for k, v in _asdict(self.limit).items() if v is not None},
            "spent": {
                "wall_ms": round(self.elapsed_ms(), 1),
                "model_calls": self.spent.model_calls,
                "cpu_ms": round(self.spent.cpu_ms, 1),
            },
        }

    def tightened(self, factor: float) -> Limit:
        """A smaller limit, for handing a sub-task a share of what is left."""
        rest = self.remaining()
        return replace(
            rest,
            wall_ms=None if rest.wall_ms is None else int(rest.wall_ms * factor),
            cpu_ms=None if rest.cpu_ms is None else int(rest.cpu_ms * factor),
        )


def _asdict(limit: Limit) -> dict[str, Any]:
    return {"wall_ms": limit.wall_ms, "model_calls": limit.model_calls, "cpu_ms": limit.cpu_ms}


@dataclass(slots=True)
class BreakerState:
    """What the breakers have seen so far in this task."""

    steps: int = 0
    retries: dict[str, int] = field(default_factory=dict)

    def note_step(self) -> None:
        self.steps += 1

    def note_retry(self, step: str) -> int:
        self.retries[step] = self.retries.get(step, 0) + 1
        return self.retries[step]

    def tripped(self, breakers: Breakers, step: str | None = None) -> str | None:
        """The name of the breaker that opened, or None.

        Returns a name rather than raising. A breaker is not an error: the
        system must still answer, with the facts it collected and a statement of
        what is missing.
        """
        if self.steps > breakers.max_steps:
            return "max_steps"
        if step is not None and self.retries.get(step, 0) > breakers.max_retries_per_step:
            return "max_retries_per_step"
        return None


__all__ = [
    "Budget",
    "BudgetClass",
    "Limit",
    "Spent",
    "Breakers",
    "BreakerState",
    "CLASS_LIMITS",
]
