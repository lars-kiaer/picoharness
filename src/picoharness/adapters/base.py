"""The adapter interface and the scope that unwinds it. Sections 7.1, 7.1.1.

Keep this interface narrow. It is what lets a new specialist model enter the
system without a change to the runtime, and every extra method is one more
thing a future adapter has to satisfy.

    load(manifest)            -> handle          # returns a scope
    run(handle, payload, ...) -> raw output
    unload(handle)                               # unwinds the scope
    probe(manifest)           -> cost and resource measurements

`probe()` is not optional. It is how section 12 fills the `null` fields of a
manifest, and it must run again after any change of hardware.

## Why the scope matters here more than elsewhere

`load()` does more than open a file. A provider can register a schema, a
grammar, a KV snapshot, a residency claim, a temporary directory and a thread
pool. Every one of these must go away at `unload()`.

This design swaps providers many times inside one task, so a registration that
does not unwind becomes a leak that grows with every step — and it will look
like a memory problem in the model, which is the wrong place to search.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..payload import Payload


class ProviderError(RuntimeError):
    """A provider could not do the work. Distinct from producing a wrong answer."""


class UnwindError(RuntimeError):
    """One or more registrations failed to unwind.

    Raised only after every other undo has been attempted. A single bad cleanup
    must not strand the rest, or the leak this class exists to prevent happens
    anyway.
    """


@dataclass(slots=True)
class Scope:
    """Everything one `load()` registered, and how to undo it.

    Undos run in reverse order, because a later registration can depend on an
    earlier one. Nothing is freed by hand.
    """

    _undos: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    _unwound: bool = False

    def register(self, label: str, undo: Callable[[], None]) -> None:
        if self._unwound:
            raise UnwindError(f"scope already unwound; cannot register {label!r}")
        self._undos.append((label, undo))

    @property
    def registered(self) -> list[str]:
        """Labels still held, in registration order. The unwind test reads this."""
        return [label for label, _ in self._undos]

    def unwind(self) -> None:
        """Undo everything, in reverse. Attempt all, then report failures."""
        failures: list[str] = []
        while self._undos:
            label, undo = self._undos.pop()
            try:
                undo()
            except Exception as exc:
                # Every undo must be attempted. A cleanup that raises must
                # not strand the ones behind it in the list.
                failures.append(f"{label}: {exc}")
        self._unwound = True
        if failures:
            raise UnwindError("; ".join(failures))


@dataclass(frozen=True, slots=True)
class Reduced:
    """What a provider produced, and how sure it was.

    A provider may return a bare record; the runtime wraps it with
    `confidence=None` and behaves exactly as before. A provider that can supply
    a calibrated score returns this instead, and the runtime can then refuse to
    commit below a declared floor — escalating **before** the work is used
    rather than after a validation failure.

    Two warnings belong with this field.

    An uncalibrated confidence is worse than none, because it looks like
    information. Section 6.9 says small models fail together on the same hard
    inputs; a model's own score is correlated with its errors in exactly the
    wrong direction, so it is confident when it is wrong.

    Therefore `how` records where the number came from, and the floor is
    **measured** and not chosen. The confidence is written into the ledger with
    the fact, so the pass rate per confidence band is a query over what actually
    happened — the same shape as section 12.4's cost model.
    """

    record: Any
    confidence: float | None = None
    how: str | None = None

    @classmethod
    def of(cls, value: Any) -> Reduced:
        """Accept either form, so an old provider needs no change."""
        return value if isinstance(value, cls) else cls(record=value)


@dataclass(slots=True)
class Handle:
    """A loaded provider. Opaque to the runtime except for the scope."""

    provider_id: str
    scope: Scope
    obj: Any = None
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Adapter(Protocol):
    """One kind of provider. Four cover most needs: code, gguf, onnx, binary."""

    kind: str

    def load(self, manifest: dict[str, Any]) -> Handle: ...

    def run(self, handle: Handle, payload: Payload, schema_id: str) -> Any: ...

    def unload(self, handle: Handle) -> None: ...

    def probe(self, manifest: dict[str, Any]) -> dict[str, Any]: ...


def timed_probe(
    adapter: Adapter,
    manifest: dict[str, Any],
    sample: Payload,
    schema_id: str,
    *,
    runs: int = 5,
) -> dict[str, Any]:
    """Measure load and run cost, for the `null` fields of a manifest.

    Cold and warm load are reported separately because section 7.2 turns on the
    difference, and section 12.2 chooses a residency policy from it. This cannot
    drop the page cache from inside the process, so a caller that wants a true
    cold number must do that first and call this once.
    """
    started = time.perf_counter()
    handle = adapter.load(manifest)
    cold_load_ms = (time.perf_counter() - started) * 1000.0

    durations: list[float] = []
    try:
        for _ in range(runs):
            at = time.perf_counter()
            adapter.run(handle, sample, schema_id)
            durations.append((time.perf_counter() - at) * 1000.0)
    finally:
        adapter.unload(handle)

    started = time.perf_counter()
    warm = adapter.load(manifest)
    warm_load_ms = (time.perf_counter() - started) * 1000.0
    adapter.unload(warm)

    durations.sort()
    return {
        "cold_load_ms": round(cold_load_ms, 2),
        "warm_load_ms": round(warm_load_ms, 2),
        "run_p50_ms": round(durations[len(durations) // 2], 2),
        "run_max_ms": round(durations[-1], 2),
        "runs": runs,
    }


__all__ = [
    "Adapter",
    "Handle",
    "Reduced",
    "Scope",
    "ProviderError",
    "UnwindError",
    "timed_probe",
]
