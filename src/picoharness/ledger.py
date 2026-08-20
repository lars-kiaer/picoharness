"""The session ledger, section 4.

One append-only file of JSON objects, one object per line. The state of the
agent is this file. Everything else in the system is a projection of it.

Four properties come from the format at no extra cost (4.1):

* **Replay.** Run the events again to get the same state.
* **Time travel.** Stop at event *n* to see the state at that moment.
* **Diff.** Compare two runs line by line.
* **Crash safety.** An append is atomic if it is smaller than one block.

The module also holds `project()`, which is the whole of section 4.5. The input
that a provider sees is a pure function of the ledger. The runtime rebuilds it
and compares before every call, so a field cannot reach a provider without
being written down first.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .payload import Payload, text
from .trust import Trust, worst

# --------------------------------------------------------------------------
# the event vocabulary
# --------------------------------------------------------------------------

#: Every event type the system may write. The memory layers in
#: `picoharness.memory` index a subset of these; a type outside this set is a
#: typo, and a typo produces a ledger that ingests silently and indexes
#: nothing. `test_ledger.py` asserts this set stays a superset of what the
#: memory layers understand, so the two cannot drift apart.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        # the spine of a session
        "composition",          # seq 0, always. See 4.6.
        "user_input",
        "plan_created",
        "step_started",
        "tool_output",
        "fact_added",
        "answer_sent",
        "declined",
        # the validation ladder, 10.2. One type per level.
        "grammar_failed",       # level 1
        "validation_failed",    # level 2
        "range_failed",         # level 3
        "crosscheck_failed",    # level 4
        "critic_rejected",      # level 5
        # the tool and the world
        "tool_failed",
        "tool_timeout",
        "tool_empty",
        # the runtime giving up, in the four ways it is allowed to
        "capability_gap",
        "breaker_tripped",
        "budget_exhausted",
        "step_failed",
        # the human, 5.4
        "approval_requested",
        "approval_denied",
    }
)


class LedgerError(RuntimeError):
    """The ledger was asked for something that would break an invariant."""


class VisibilityViolation(LedgerError):
    """Something reached a provider that the ledger cannot account for.

    This is principle P12. It is an assertion and not a habit, because the bug
    it catches is invisible: a prompt grows a field that nobody logged, and the
    replay then produces a different answer with no visible cause.
    """


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------


class Clock(Protocol):
    """Where a timestamp comes from. A seam, so that replay can be exact."""

    def __call__(self) -> str: ...


def utc_now() -> str:
    """Second resolution, UTC, sortable as a string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReplayClock:
    """Hand back the timestamps that a previous run recorded.

    Section 10.3 asks for deterministic replay, not transcript replay. A wall
    clock makes that impossible on its own, because time is an input to the run
    and it never repeats. So a replay feeds the recorded times back in.

    Running out of timestamps is not an error to hide. It means the replay
    produced more events than the original, which is a divergence, and it is
    better to hear about it here than to compare two files afterwards.
    """

    def __init__(self, events: Iterable[dict[str, Any]]) -> None:
        self._times = [e.get("t", "") for e in events]
        self._i = 0

    def __call__(self) -> str:
        if self._i >= len(self._times):
            raise LedgerError(
                f"replay produced event {self._i}, but the original ledger has "
                f"only {len(self._times)}: the runs have diverged"
            )
        out = self._times[self._i]
        self._i += 1
        return out

    @property
    def exhausted(self) -> bool:
        return self._i >= len(self._times)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def read_events(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield the events of one ledger. A bad line stops the read loudly.

    Silence is the wrong answer here. A ledger with one unreadable line is a
    ledger you cannot replay, and pretending otherwise moves the failure to
    somewhere that it makes no sense.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} is not valid JSON") from exc


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


class Ledger:
    """An append-only event file for one session.

    Open it, append to it, close it. There is no update and no delete. The
    current state is derived by reading from the start, which is cheap at the
    size that one session reaches, and it is the reason a crash costs nothing.
    """

    def __init__(
        self,
        session_dir: str | Path,
        *,
        session_id: str | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.dir = Path(session_dir)
        self.path = self.dir / "events.jsonl"
        self.blob_dir = self.dir / "blobs"
        self.session_id = session_id or self.dir.name
        self.clock = clock
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq = self._resume()
        self._fh = self.path.open("a", encoding="utf-8", newline="\n")

    def _resume(self) -> int:
        """Continue after a crash. All state is on disk, so this is a read."""
        if not self.path.exists():
            return 0
        last = -1
        for event in read_events(self.path):
            last = max(last, int(event.get("seq", -1)))
        return last + 1

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def next_seq(self) -> int:
        return self._seq

    # -- append ------------------------------------------------------------

    def append(self, event_type: str, **fields: Any) -> int:
        """Write one event. Returns its sequence number.

        The write is one `write()` of one line, then a flush and an `fsync`.
        That is what makes the append atomic, and atomic is what makes a power
        loss cost nothing more than the step in progress.
        """
        if event_type not in EVENT_TYPES:
            raise LedgerError(
                f"unknown event type {event_type!r}. Add it to EVENT_TYPES and teach "
                f"the memory layers about it, or the event indexes to nothing."
            )
        if self._fh.closed:
            raise LedgerError("the ledger is closed")
        if self._seq == 0 and event_type != "composition":
            raise LedgerError(
                f"event 0 must be `composition`, not {event_type!r}. Section 4.6: a "
                f"run that does not record what it was made of cannot be audited."
            )

        event: dict[str, Any] = {
            "seq": self._seq,
            "t": self.clock(),
            "type": event_type,
            "session_id": self.session_id,
        }
        event.update({k: v for k, v in fields.items() if v is not None})
        self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._seq += 1
        return int(event["seq"])

    # -- blobs -------------------------------------------------------------

    def write_blob(self, name: str, data: str | bytes) -> str:
        """Put raw tool output beside the ledger and return its relative path.

        Raw output does not belong inline. It is large, it is untrusted, and
        the ledger stays greppable only if it holds references to big things
        rather than the things themselves.
        """
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        target = self.blob_dir / name
        if isinstance(data, str):
            target.write_text(data, encoding="utf-8")
        else:
            target.write_bytes(data)
        return str(Path("blobs") / name).replace("\\", "/")

    def read_blob(self, ref: str) -> bytes:
        return (self.dir / ref).read_bytes()

    # -- read back ---------------------------------------------------------

    def events(self) -> list[dict[str, Any]]:
        """Everything written so far, in order.

        Works on a closed ledger too. The ledger is a file, and reading a
        finished session is the ordinary case — the memory layers do nothing
        else. Only a live handle needs flushing first.
        """
        if not self._fh.closed:
            self._fh.flush()
        return list(read_events(self.path)) if self.path.exists() else []


# --------------------------------------------------------------------------
# the projection, section 4.5
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderInput:
    """Exactly what one provider is about to be given.

    Two of these compare equal when the provider would see the same thing. That
    comparison is the point: the runtime holds one, rebuilds another from the
    ledger, and refuses to continue if the two differ.
    """

    step: str
    capability: str
    schema_id: str
    payload: Payload
    facts: tuple[tuple[str, str], ...] = ()

    @property
    def trust(self) -> Trust:
        """The level this call operates at. Inherited, never upgraded."""
        return worst([self.payload.trust, *(t for _, t in self.facts)])

    def digest(self) -> str:
        """A stable rendering, for comparison and for an error message."""
        body = self.payload.data
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        return json.dumps(
            {
                "step": self.step,
                "capability": self.capability,
                "schema": self.schema_id,
                "mime": self.payload.mime,
                "trust": self.payload.trust,
                "body": body,
                "facts": list(self.facts),
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def project(
    events: Sequence[dict[str, Any]],
    *,
    step: str,
    capability: str,
    schema_id: str,
    blob_reader: Any = None,
    max_trust_in: Trust = "T1",
) -> ProviderInput:
    """Rebuild a provider's input from the ledger alone.

    This function reads no state that is not an event. That is the property the
    visibility invariant needs, and it is also why the function can be tested
    on its own, with no model and no tool.

    `max_trust_in` comes from the provider manifest. A fact above the ceiling is
    left out here rather than filtered later, so that the input the runtime
    compares against is the input the provider will actually receive.
    """
    step_trust: Trust = "T1"
    body: str | bytes = ""
    mime = "text/plain"
    facts: list[tuple[str, str]] = []
    allowed = {"T2"} if max_trust_in == "T2" else {"T0", "T1", "T2"}

    # A `fact_added` event does not carry a trust level of its own. The tool
    # declared one on `step_started`, and the fact inherits it. This mirrors
    # `_EpisodeBuilder.consume()` in the episodic index; the two must agree, or
    # the projection and the store disagree about what a provider may read.
    trust_of_step: dict[str, str] = {}

    for event in events:
        etype = event.get("type")
        if etype == "step_started":
            trust_of_step[event.get("step", "")] = event.get("trust", "T1")
            if event.get("step") == step:
                step_trust = event.get("trust", "T1")
        elif etype == "tool_output" and event.get("step") == step:
            mime = event.get("mime", "text/plain")
            if event.get("blob") and blob_reader is not None:
                body = blob_reader(event["blob"])
            else:
                body = event.get("text", "")
        elif etype == "fact_added" and event.get("step") != step:
            trust = event.get("trust") or trust_of_step.get(event.get("step", ""), "T1")
            if trust in allowed:
                payload_json = json.dumps(
                    event.get("fact", {}), sort_keys=True, ensure_ascii=False
                )
                facts.append((payload_json, trust))

    payload = (
        Payload(data=body, mime=mime, trust=step_trust) if body else text("", trust=step_trust)
    )
    return ProviderInput(
        step=step,
        capability=capability,
        schema_id=schema_id,
        payload=payload,
        facts=tuple(facts),
    )


#: Fields that record what the machine did, not what the function returned.
#:
#: Section 10.3 asks for deterministic replay: the same inputs produce the same
#: result. A duration is not a result. It is an observation of a machine under
#: whatever load it was under, and it never repeats — so a replay reproduces
#: every field except these, and saying so is what keeps the claim testable.
#:
#: They stay in the ledger because section 12.4 needs them: every provider call
#: writes its duration, and that is what makes the cost model a query rather
#: than a new subsystem.
MEASURED_FIELDS: frozenset[str] = frozenset({"duration_ms", "cpu_ms", "spent"})


def result_of(event: dict[str, Any]) -> dict[str, Any]:
    """One event with its measurements removed."""
    return {k: v for k, v in event.items() if k not in MEASURED_FIELDS}


def first_difference(
    left: Sequence[dict[str, Any]], right: Sequence[dict[str, Any]]
) -> str | None:
    """Where two runs stopped agreeing, ignoring measurements. None if they agree.

    Returns a description rather than a bool, because "the replay diverged" is
    not a useful thing to be told at three in the morning.
    """
    if len(left) != len(right):
        return f"different lengths: {len(left)} events against {len(right)}"
    for index, (a, b) in enumerate(zip(left, right, strict=True)):
        ra, rb = result_of(a), result_of(b)
        if ra != rb:
            fields = sorted({k for k in set(ra) | set(rb) if ra.get(k) != rb.get(k)})
            return (
                f"event {index} ({a.get('type')}) differs on {fields}: "
                f"{ {k: ra.get(k) for k in fields} } against { {k: rb.get(k) for k in fields} }"
            )
    return None


def assert_visible(held: ProviderInput, rebuilt: ProviderInput) -> None:
    """Refuse to call a provider with something the ledger cannot explain.

    Call this immediately before every provider call. It costs one comparison,
    and it removes a class of bug that is otherwise found months later, in a
    replay that does not match and gives no reason why.
    """
    if held.digest() != rebuilt.digest():
        raise VisibilityViolation(
            "the input held does not match the input rebuilt from the ledger.\n"
            f"  held:     {held.digest()}\n"
            f"  rebuilt:  {rebuilt.digest()}"
        )


__all__ = [
    "Ledger",
    "EVENT_TYPES",
    "LedgerError",
    "VisibilityViolation",
    "Clock",
    "ReplayClock",
    "utc_now",
    "read_events",
    "ProviderInput",
    "project",
    "assert_visible",
    "MEASURED_FIELDS",
    "result_of",
    "first_difference",
]
