"""Waterfall hooks at the phase boundaries, section 5.1.1.

Do not write the validation ladder and the trust filter into the body of the
loop. Make each phase boundary a hook point. A listener can observe the value,
change it, or reject it, and it passes control on by calling `next()`.

```
SELECT --[on_select]--> PREPARE --[on_prepare]--> EXECUTE
   --[on_output]--> REDUCE --[on_reduce]--> VALIDATE
   --[on_commit]--> COMMIT
```

This keeps the loop short, and it lets a check be added without touching the
loop — which is the same test section 17 applies to the runtime as a whole.

Two rules protect the design, and both are enforced here rather than trusted:

1. **A hook must never be a provider.** A hook is deterministic code. If it
   needs a model it is not a hook; it is a step, and it belongs in the plan.
   Hooks are therefore given the value and nothing else — no registry, no
   ledger, no way to reach a provider.
2. **A hook that rejects must write an event.** A silent rejection is a bug you
   cannot find later, so rejecting means raising `Rejected` with the event that
   describes it, and the runtime writes that event before it stops the step.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

Point = Literal["on_select", "on_prepare", "on_output", "on_reduce", "on_commit"]

POINTS: tuple[str, ...] = ("on_select", "on_prepare", "on_output", "on_reduce", "on_commit")

#: A listener takes the value and a `next` callable. It must call `next(value)`
#: to pass control on, and what it returns is what the next stage sees.
Listener = Callable[[Any, Callable[[Any], Any]], Any]


class HookError(RuntimeError):
    """The hook system was used in a way that breaks one of the two rules."""


class Rejected(Exception):
    """A listener refused the value.

    It carries the event that must be written. There is no way to reject
    without one, which is rule 2 made structural: you cannot express a silent
    rejection.
    """

    def __init__(self, event_type: str, **fields: Any) -> None:
        self.event_type = event_type
        self.fields = fields
        detail = fields.get("error") or fields.get("reason") or event_type
        super().__init__(str(detail))


class Hooks:
    """The listeners registered at each phase boundary."""

    __slots__ = ("_at",)

    def __init__(self) -> None:
        self._at: dict[str, list[tuple[str, Listener]]] = {p: [] for p in POINTS}

    def on(self, point: str, listener: Listener, *, name: str | None = None) -> Hooks:
        """Register a listener. Order of registration is order of execution."""
        if point not in self._at:
            raise HookError(f"unknown hook point {point!r}; expected one of {POINTS}")
        self._at[point].append((name or getattr(listener, "__name__", "anonymous"), listener))
        return self

    def listeners(self, point: str) -> list[str]:
        """The names at one point, in order. For the composition document."""
        return [name for name, _ in self._at.get(point, [])]

    def resolve(self) -> dict[str, list[str]]:
        """Everything registered, for the composition hash of section 4.6.

        A different set of hooks is a different system, so it belongs in the
        hash. Without this, adding a filter would change behaviour and leave the
        two runs looking identical.
        """
        return {point: self.listeners(point) for point in POINTS if self._at[point]}

    def run(self, point: str, value: Any) -> Any:
        """Pass the value down the chain. Returns what comes out the far end.

        A listener that does not call `next()` stops the chain and its return
        value is used. That is deliberate: short-circuiting is a legitimate
        thing for a filter to do. Rejecting is different, and raises.
        """
        chain = self._at.get(point, ())
        if not chain:
            return value

        def step(index: int, current: Any) -> Any:
            if index >= len(chain):
                return current
            name, listener = chain[index]
            called = False

            def nxt(passed: Any) -> Any:
                nonlocal called
                if called:
                    raise HookError(f"{name} at {point} called next() more than once")
                called = True
                return step(index + 1, passed)

            return listener(current, nxt)

        return step(0, value)


# --------------------------------------------------------------------------
# listeners the runtime always installs
# --------------------------------------------------------------------------


def size_limit(max_bytes: int) -> Listener:
    """`on_output`: cut oversized tool output, and leave a trace.

    Section 5.3 makes `max_bytes_to_model` a breaker. This truncates rather than
    rejecting, because a long log is not an error — it is the normal case, and
    the first part of it usually holds the answer.
    """

    def listener(payload: Any, nxt: Callable[[Any], Any]) -> Any:
        return nxt(payload.truncated(max_bytes))

    listener.__name__ = f"size_limit({max_bytes})"
    return listener


def trust_filter(max_trust_in: str) -> Listener:
    """`on_prepare`: refuse to hand a provider data above its ceiling.

    This is section 11.2 in one place. The provider manifest declares what it
    may read; a payload above that never reaches it. Rejecting writes an event,
    so a refusal is visible in the ledger rather than being a silent no-op.
    """
    from .trust import TRUST_ORDER

    def listener(value: Any, nxt: Callable[[Any], Any]) -> Any:
        incoming = value.trust if hasattr(value, "trust") else "T1"
        if TRUST_ORDER[incoming] > TRUST_ORDER[max_trust_in]:
            raise Rejected(
                "validation_failed",
                kind="schema",
                error=f"input is {incoming}, provider accepts at most {max_trust_in}",
                detail_trust="T2",
            )
        return nxt(value)

    listener.__name__ = f"trust_filter({max_trust_in})"
    return listener


__all__ = [
    "Hooks",
    "Listener",
    "Point",
    "POINTS",
    "Rejected",
    "HookError",
    "size_limit",
    "trust_filter",
]
