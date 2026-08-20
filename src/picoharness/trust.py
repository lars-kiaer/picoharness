"""Trust levels, section 11.1.

Three levels, and one rule that the rest of the system depends on:

    A fact carries the trust level of its source, for ever.

Nothing here upgrades a level. The only operations are "what is the worst of
these" and "may this reach the control plane". Both are deliberately dull.

This module owns the vocabulary. `picoharness.memory.episodic` imports it, so
that the store and the runtime cannot disagree about what T1 means.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

Trust = Literal["T0", "T1", "T2"]

#: T0 the user, T1 outside the system, T2 the system itself.
TRUST_LEVELS: tuple[str, ...] = ("T0", "T1", "T2")

#: Sort order for "which of these is worst". T1 is worst because it came from
#: outside. T2 is best because the validation ladder produced it.
TRUST_ORDER: dict[str, int] = {"T2": 0, "T0": 1, "T1": 2}

#: The levels a control capability (`plan@1`, `route@1`) may read. See 11.2.
CONTROL_SAFE: frozenset[str] = frozenset({"T0", "T2"})


class TrustError(ValueError):
    """An unknown trust level, or one used where it is not allowed."""


def check(level: str) -> Trust:
    """Reject a level that is not one of the three. A typo must not pass."""
    if level not in TRUST_ORDER:
        raise TrustError(f"unknown trust level {level!r}; expected one of {TRUST_LEVELS}")
    return level  # type: ignore[return-value]


def worst(levels: Iterable[str]) -> Trust:
    """The level that a value derived from all of these must carry.

    Trust is inherited, so a record built from T2 and T1 inputs is T1. There is
    no averaging and no majority.
    """
    seen = [check(level) for level in levels]
    if not seen:
        return "T2"
    return max(seen, key=lambda level: TRUST_ORDER[level])


def may_control(level: str) -> bool:
    """May a provider that read data at this level decide what happens next?

    This is principle P7 in one line. A provider that read T1 must not emit a
    control decision, because the T1 data can be an instruction written by an
    attacker.
    """
    return check(level) in CONTROL_SAFE


__all__ = [
    "Trust",
    "TRUST_LEVELS",
    "TRUST_ORDER",
    "CONTROL_SAFE",
    "TrustError",
    "check",
    "worst",
    "may_control",
]
