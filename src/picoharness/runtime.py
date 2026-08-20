"""The step lifecycle and the main loop. Sections 5.1 and 5.2.

    SELECT -> PREPARE -> EXECUTE -> REDUCE -> VALIDATE -> COMMIT

Four of the six phases need no model. That is the point of the design.

| Phase | What happens | Model? |
|-------|--------------|--------|
| SELECT | Find the next `pending` step | No |
| PREPARE | Build the tool arguments | Sometimes |
| EXECUTE | Run the tool in the world | No |
| REDUCE | Convert raw output to a schema | Often |
| VALIDATE | Grammar, schema, types, ranges | No |
| COMMIT | Append a `fact_added` event | No |

The loop terminates, the loop is code, and it can be unit-tested with a fake
provider. In v1 there is no planner: a plan is handed in. The planner is a
`plan@1` provider and arrives at v4, which is why `call_planner()` in section
5.2 appears here as a single, obvious gap rather than as a stub that pretends.

Two invariants are enforced rather than trusted. Before every provider call the
input is rebuilt from the ledger and compared (section 4.5). And a provider that
has read T1 data cannot serve a control capability, because `select()` filters
on the trust ceiling before the call is made (section 11.2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .adapters.base import ProviderError
from .budget import BreakerState, Budget
from .hooks import Hooks, Rejected
from .ledger import Ledger, ProviderInput, assert_visible, project
from .payload import Payload
from .registry import CapabilityGap, Registry
from .validate import CrossChecks, Schema, ladder
from .world import World, WorldError


@dataclass(slots=True)
class Tool:
    """One tool, per the manifest of section 8.1.

    The tool declares its own reducer. The dispatcher does not know how a log
    looks; it only knows the goal.
    """

    name: str
    run: Callable[[World, dict[str, Any]], Payload]
    reducer: str = "extract@1"
    output_schema: str = "unknown@0"
    effect: str = "read_only"
    idempotent: bool = True
    trust_out: str = "T1"
    version: int = 1

    def resolve(self) -> dict[str, Any]:
        return {
            "reducer": self.reducer,
            "output_schema": self.output_schema,
            "effect": self.effect,
            "idempotent": self.idempotent,
            "trust_out": self.trust_out,
            "version": self.version,
        }


@dataclass(slots=True)
class Step:
    id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    subject: str | None = None
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class Outcome:
    """How the task ended, and what it managed to collect."""

    status: str
    answer: str | None
    facts: tuple[dict[str, Any], ...]
    missing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "answered"


class Runtime:
    """The only component that makes a decision. Everything else is a resource."""

    def __init__(
        self,
        ledger: Ledger,
        registry: Registry,
        world: World,
        *,
        tools: dict[str, Tool],
        schemas: dict[str, Schema],
        budget: Budget | None = None,
        hooks: Hooks | None = None,
        cross_checks: dict[str, CrossChecks] | None = None,
    ) -> None:
        self.ledger = ledger
        self.registry = registry
        self.world = world
        self.tools = tools
        self.schemas = schemas
        self.budget = budget or Budget()
        self.hooks = hooks or Hooks()
        self.cross_checks = cross_checks or {}
        self.breakers = BreakerState()

    # -- the loop, section 5.2 --------------------------------------------

    def run(self, goal: str, plan: list[Step]) -> Outcome:
        """Run a plan to its end. Always returns; never leaves the caller waiting."""
        self.ledger.append("user_input", text=goal)
        self.ledger.append("plan_created", steps=[s.id for s in plan])

        missing: list[str] = []
        for step in plan:
            if self.budget.exceeded():
                self.ledger.append("budget_exhausted", step=step.id, spent=self.budget.to_json())
                missing.append(step.id)
                break

            self.breakers.note_step()
            tripped = self.breakers.tripped(self.budget.breakers)
            if tripped:
                self.ledger.append("breaker_tripped", step=step.id, reason=tripped)
                missing.append(step.id)
                break

            if not self._run_step(step):
                missing.append(step.id)
                # A failed step is not the end of the task. Section 5.2 calls the
                # planner here; until v4 there is none, so the loop carries on
                # with the steps it has and answers with what it collected.

        return self._answer(goal, missing)

    def _run_step(self, step: Step) -> bool:
        """One step through all six phases. True when a fact was committed."""
        tool = self.tools.get(step.tool)
        if tool is None:
            self.ledger.append(
                "tool_failed", step=step.id, tool=step.tool, error="no such tool"
            )
            return False

        self.ledger.append(
            "step_started",
            step=step.id,
            tool=tool.name,
            trust=tool.trust_out,
            subject=step.subject,
        )

        # PREPARE, then EXECUTE.
        try:
            args = self.hooks.run("on_prepare", dict(step.args))
            raw = tool.run(self.world, args)
        except Rejected as rejected:
            self.ledger.append(rejected.event_type, step=step.id, tool=tool.name, **rejected.fields)
            return False
        except WorldError as exc:
            self.ledger.append(
                "tool_failed", step=step.id, tool=tool.name,
                capability=tool.reducer, error=str(exc),
            )
            return False

        raw = self.hooks.run("on_output", raw)
        if raw.nbytes == 0:
            self.ledger.append("tool_empty", step=step.id, tool=tool.name)
            return False

        blob = self.ledger.write_blob(f"{step.id}.raw", raw.data)
        self.ledger.append(
            "tool_output", step=step.id, blob=blob, bytes=raw.nbytes, mime=raw.mime
        )

        # REDUCE, VALIDATE, COMMIT — with retries and then escalation.
        return self._reduce(step, tool, raw)

    def _reduce(self, step: Step, tool: Tool, raw: Payload) -> bool:
        """Turn raw output into a validated fact, escalating when it does not."""
        tried: set[str] = set()
        while True:
            try:
                provider = self.registry.select(
                    tool.reducer,
                    schema=tool.output_schema,
                    trust_in=raw.trust,
                    mime=raw.mime,
                    exclude=frozenset(tried),
                )
            except CapabilityGap as gap:
                self.ledger.append(
                    "capability_gap", step=step.id, capability=tool.reducer, reason=str(gap)
                )
                return False

            tried.add(provider.id)
            attempt = self.breakers.retries.get(step.id, 0) + 1
            record, duration_ms, error = self._call(step, tool, provider, raw)

            if error is None and record is not None:
                result = self._validate(record, tool, raw)
                if result.ok:
                    self.ledger.append(
                        "fact_added",
                        step=step.id,
                        provider=provider.id,
                        schema=tool.output_schema,
                        duration_ms=round(duration_ms, 2),
                        fact=self.hooks.run("on_commit", record),
                    )
                    self.budget.charge(wall_ms=duration_ms)
                    return True
                error, kind = result.error(), result.kind
            else:
                kind = "tool_error"

            self.ledger.append(
                "validation_failed" if kind != "tool_error" else "tool_failed",
                step=step.id, tool=tool.name, capability=tool.reducer,
                provider=provider.id, schema=tool.output_schema,
                attempt=attempt, duration_ms=round(duration_ms, 2),
                kind=kind, error=error,
            )
            self.budget.charge(wall_ms=duration_ms)

            # Section 6.5: retry, then escalate, then decline. The ladder ends
            # inside the box; a decline is a valid outcome.
            if self.breakers.note_retry(step.id) > self.budget.breakers.max_retries_per_step:
                self.ledger.append(
                    "step_failed", step=step.id, tool=tool.name, reason="retries exhausted"
                )
                return False

    def _call(
        self, step: Step, tool: Tool, provider: Any, raw: Payload
    ) -> tuple[dict[str, Any] | None, float, str | None]:
        """Run one provider, having first proved the ledger explains its input."""
        from .adapters.code import time_call

        rebuilt = project(
            self.ledger.events(),
            step=step.id,
            capability=tool.reducer,
            schema_id=tool.output_schema,
            blob_reader=self.ledger.read_blob,
            max_trust_in=provider.max_trust_in,  # type: ignore[arg-type]
        )
        # Section 4.5. `held` carries what the runtime has in hand; `rebuilt`
        # carries only what the ledger can account for. The facts are taken from
        # `rebuilt` on purpose, so the comparison isolates the payload — which is
        # the channel a value can actually escape through. If a hook were ever to
        # change the output after the blob was written, the two would diverge
        # here rather than in a replay months later that gives no reason why.
        held = ProviderInput(
            step=step.id,
            capability=tool.reducer,
            schema_id=tool.output_schema,
            payload=raw,
            facts=rebuilt.facts,
        )
        assert_visible(held, rebuilt)

        adapter = self.registry.adapter_for(provider)
        handle = adapter.load(provider.manifest)
        try:
            record, duration_ms = time_call(
                lambda: adapter.run(handle, rebuilt.payload, tool.output_schema)
            )
            return record, duration_ms, None
        except ProviderError as exc:
            return None, 0.0, str(exc)
        finally:
            adapter.unload(handle)  # P13: every load unwinds, including on failure

    def _validate(self, record: Any, tool: Tool, raw: Payload) -> Any:
        """The ladder of section 10.2, levels 2 to 4."""
        schema = self.schemas.get(tool.output_schema)
        if schema is None:
            from .validate import OK

            return OK
        return ladder(
            record,
            schema,
            raw_input=raw.as_text() if raw.is_text else None,
            cross_checks=self.cross_checks.get(tool.output_schema),
        )

    # -- the answer --------------------------------------------------------

    def _answer(self, goal: str, missing: list[str]) -> Outcome:
        """Answer with what was collected, and name what is not there.

        A partial answer with a named gap is better than a timeout, and a
        breaker opening must never mean silence.
        """
        facts = tuple(
            e["fact"] for e in self.ledger.events() if e.get("type") == "fact_added"
        )
        try:
            provider = self.registry.select("answer@1", trust_in="T2")
        except CapabilityGap as gap:
            self.ledger.append("declined", reason=str(gap))
            return Outcome("declined", None, facts, tuple(missing))

        adapter = self.registry.adapter_for(provider)
        handle = adapter.load(provider.manifest)
        try:
            payload = Payload(
                data=_render_question(goal, facts, missing), mime="application/json", trust="T2"
            )
            answer = adapter.run(handle, payload, "answer@1")
        except ProviderError as exc:
            self.ledger.append("declined", reason=str(exc))
            return Outcome("declined", None, facts, tuple(missing))
        finally:
            adapter.unload(handle)

        status = "partial" if missing else "answered"
        self.ledger.append("answer_sent", outcome=status, text=answer)
        return Outcome(status, answer, facts, tuple(missing))


def _render_question(goal: str, facts: tuple[Any, ...], missing: list[str]) -> str:
    import json

    return json.dumps(
        {"goal": goal, "facts": list(facts), "missing": missing},
        sort_keys=True,
        ensure_ascii=False,
    )


__all__ = ["Runtime", "Step", "Tool", "Outcome"]
