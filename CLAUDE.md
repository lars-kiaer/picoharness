# picoharness — working rules

A serial micro-agent harness for a CPU-only edge box. The state is a file, models
are pure functions over it, and one provider runs at a time.

Full design: `docs/serial-micro-agent-harness.md` (v0.9).
Model and runtime choices: `docs/model-evaluation-and-runtime.md`.
Read the section named in a rule before you argue with the rule.

## Where the project actually is

**v1 and most of v2 are complete, and v6 was built first.** A three-step task
runs end to end and replays identically with no model involved, and the same
task runs against a real model when one manifest is edited. The ledger either
run writes ingests into the memory layers that were built before them.

| Stage | State |
|---|---|
| v0 measurements | Waiting on hardware. Nothing measured yet |
| **v1** ledger, loop, `code` provider | **Done.** `tests/test_runtime.py` is the exit test |
| — instruction record §10.3 | **Done.** `prompt.py`, `prompts/extract-log-summary.md` |
| v2 `gguf` adapter, swap test, system channel | **Adapter and swap test done** (`tests/test_gguf.py`). The system channel of §11.2 is not started |
| v3 selection policy, budgets, JIT tools | **Half done.** `select()` implements all six filters of §6.4; JIT tool retrieval is not started |
| v4 planner, escalation | `call_planner()` is an open gap in `runtime.run()`, deliberately not stubbed |
| v5 the three probe tasks | Not started |
| v6 memory layers | Done, with a real producer and now a real consumer |
| v7 security suite, sandbox | The world seam exists; `LocalWorld` runs no subprocess on purpose |

Also done, out of order: the 30-fixture set for `extract@1`, a `code` provider
that scores **29 of 30** against it, and the validation ladder at levels 2–4.
The one it cannot reach is `absent-06`, where an error line must be recognised
with no severity word anywhere in the input. That is the single clearest case
for a model in the whole set.

The core is at **2,083 lines of code** against the 2,000-line budget of §17,
counting `src/picoharness/*.py` and `adapters/`, but not `providers/` (189) or
`memory/` (1,257). **That is 83 over, and it needs a decision, not a trim.**
Nothing in the excess is a special case in the runtime: it is `prompt.py` (152)
and `adapters/gguf.py` (124), and the design specifies both. So either the
budget was set before the design was finished, or the boundary is in the wrong
place. The case for moving it: `code.py` and `gguf.py` are implementations
behind the adapter seam, exactly as `providers/` are implementations behind the
capability seam, and a box that runs no model ships neither. Counted that way
the core is **1,899**. Settle it before v3 adds more.

## What to pick up next

1. **Finish v2: the system channel of §11.2.** A typed environment record for
   the system slot, never prose, and a fourth kind of poisoned fixture — an
   instruction inside the environment record itself. It needs no hardware.
2. **The seven rulings in `fixtures/README.md` are settled** (2026-08-20). Do
   not reopen one after the first evaluation run: a convention changed later
   invalidates every pass rate measured under the old one. Ruling 7 narrowed a
   service start to the form the init system writes, which moved `adv-02` from
   `true` to `false` and took the code baseline from 28 to 29.
3. **CI runs and passes.** Five runs on Ubuntu against 3.11 and 3.14, green,
   the newest on `3a4d997` (checked 2026-08-20 through the GitHub API, because
   `gh` is not installed on this box). The declared floor really does execute
   the suite.
4. **§16 still has six open decisions**, and question 3 — how models arrive on
   the box — now blocks more than it did: Needle fetches its engine over the
   network, and `llama-cpp-python` pulls five packages besides itself, so an
   offline ARM board means six wheels carried by hand or a compile run there.
5. **The prompt is the next real measurement.** At the shipped instruction the
   350M model gets roughly one field in seven on four fixtures, against 29 of
   30 for the parser. That is not yet a verdict: it is one prompt, measured
   outside the ledger. Run it through the adapter over all 30 fixtures and read
   `v_provider_health`, which is what the whole loop was built for.

## Rules that decide code review

| Rule | Meaning here |
|---|---|
| **P1** state is a file | No provider keeps state between calls. All state on disk. |
| **P2** the runtime selects | Control flow is code, never a model decision. |
| **P3** deterministic code first | A log filter is `grep`. A table sum is SQL. A schema check is a parser. Register those as `code` providers; the policy prefers them. |
| **P5** one model at a time | Keeps the memory profile flat. |
| **P6** every step repeatable | Pin weights, quantization, sampler, schema, composition hash. |
| **P7** T1 data cannot start an action | A provider that read untrusted data must not emit control. |
| **P9** no network at run time | Provisioning is a separate step. No cloud fallback, ever. |
| **P11** keep the core small | Under 2,000 lines. Special case in the runtime? It probably belongs in a manifest. |
| **P12** provider-visible means logged | Rebuild the input from the ledger and assert it matches before every call (§4.5). |
| **P13** registration must unwind | `load()` returns a scope; `unload()` unwinds it in reverse. |

## Non-negotiable properties

- **Zero runtime dependencies.** `dependencies = []` in `pyproject.toml` is
  deliberate and CI asserts it. Everything is stdlib: SQLite with FTS5 ships with
  Python. Every dependency is one more thing to provision by hand on an offline
  box. Dev-only tools (pytest, ruff) belong in `[dev]`.
- **The memory layers are derived.** Delete `episodic.db` and rebuild from the
  ledgers. `test_rebuild_*` asserts nothing is lost. If a layer cannot be rebuilt,
  state escaped the ledger and P12 is broken.
- **Trust is inherited and permanent.** A fact from T1 data is T1 for ever.
  `recall()` serves anyone; `recall_for_control()` serves the planner and router
  and raises `TrustViolation` on a leak. Never add a third path.
- **No side door into memory.** A fact enters through COMMIT and the validation
  ladder, or it does not exist.
- **Path 1 first.** Structured SQL and BM25 answer most questions with no model.
  Do not add vector search until measurement shows path 1 is not enough — changing
  the embedder invalidates the whole index, which is hours on a CPU.
- **Narrow the output space until the attack has no representation** (section
  11.2). This is one method used in three places, and none of it depends on a
  model behaving well: a reducer cannot emit a tool call because the schema has
  no field for one; a model cannot name an unselected tool because the grammar
  has no production for it; a model cannot invent a host name because an
  extractive value must have a span in the input.
- **Abstention is a contract** (10.2.1). Every schema field accepts `null`, and
  a field marked `x-extractive` must appear in the input verbatim when it is not
  null. `null` is never span-checked — it is the honest answer.
- **Security is declared per capability, not per provider** (6.2). One provider
  may serve `extract@1` over T1 and `route@1` over T0 only. A capability the
  manifest does not name gets `T2`: silence must not widen a boundary.
- **The configuration plane is checked like the data plane** (6.3, 8.1). A
  manifest is an instance of `manifest@1` with `additionalProperties: false`,
  and a tool's arguments are checked against its `input_schema`. The defect this
  closes: writing `secuirty` missed the whole block, so `max_trust_in` fell back
  to its permissive default — a typo that made a security boundary *wider*.
- **An instruction is a typed record with one free-text field** (10.3). Field
  rules live in the schema and are rendered in; prose carries what a schema
  cannot say. The prose is written at build time and never at run time, so a
  planner has nowhere to put an instruction — only typed, trust-carrying
  parameters.
- **The topology is serial by decision, not by default** (3.1). Concurrency
  would cost the residency policy, line-by-line diffing of two runs, and the
  ledger being the causal history rather than a reconstruction of it.
- **The policy reads the ledger, once** (6.4, 12.4). `select()` filters on
  measured pass rate and measured cost, from `read_measurements()`. The numbers
  are frozen for the task and written to the ledger as `policy_snapshot`, so a
  replay routes on what the run saw and not on what the database holds today.
  Below five calls there is no measurement, only a count, and neither filter may
  act on it.
- **KV snapshots are made at provisioning, not during evaluation** (7.4). The
  fixed prefix is 600–800 tokens and its snapshot is 20–35 MB, so a read costs
  ~20 ms against seconds of prefill. But the prefix moves while the prompt is
  being tuned, and a bake-off has ten to twenty candidates, so generating a
  library before the prompt settles means regenerating it every iteration.
  Retrieval and prefix caching also pull against each other (8.2): cache what
  comes before the tool block, prefill the block.
- **A missing confidence is not a low confidence** (6.5). A provider that
  reports no score is never gated, and a floor is measured from the ledger, not
  chosen. An uncalibrated score is worse than none because it looks like
  information.

## The event vocabulary is already fixed

The ledger writer must emit exactly the event types the memory layers understand,
or ingest silently sees nothing. The authority is `EVENT_TO_KIND` in
`memory/failure.py` and `_EpisodeBuilder.consume()` in `memory/episodic.py`:

```
composition  user_input  step_started  fact_added  answer_sent  declined
plan_created  validation_failed  grammar_failed  range_failed
crosscheck_failed  critic_rejected  tool_failed  tool_timeout  tool_empty
capability_gap  breaker_tripped  budget_exhausted  approval_denied  step_failed
```

Two types are written but not indexed, and that is deliberate: `tool_output`
points at a blob, and `policy_snapshot` records what the policy had measured
before it chose. `ledger.EVENT_TYPES` is the writer's vocabulary and stays a
superset of the list above; a name outside it is refused at `append()`.

Field requirements worth remembering: `step_started` carries `trust` and
`subject`; `fact_added` carries `provider`, `schema`, `duration_ms`,
`valid_until`. Without `duration_ms` the cost model has nothing to measure.

`memory/samples.py` is the executable specification of the format. Treat it as the
contract.

## The model box

v2 needs Linux, and WSL2 `Ubuntu-24.04` is it (Python 3.12.3, gcc 13.3, cmake
3.28). Never publish a measurement from there: it is a VM with dynamic memory,
so its load times are not edge numbers. Use
`MachineProfile.detect("wsl2-x86", representative=False)`.

```bash
wsl -d Ubuntu-24.04 -u root -- bash -c "cd ~/picoharness && .venv/bin/python -m pytest -q"
```

Weights live in `~/models` inside that distro and never in the repository. The
manifest names the file relative to the model root and pins it by sha256, so
the composition hash stays the same on every box while the pin stays real.
`tests/test_gguf.py` skips when the weights or the binding are missing, which
is every Windows machine and every CI runner. Provisioning is a separate step
from running, and P9 is why.

## Hardware

Three tiers, two purchases. Each answers a different question, and each produces
its own machine hash against the same composition — which is section 4.6's
warning demonstrated rather than described.

| Tier | Board | Its job |
|---|---|---|
| Low | Orange Pi Lite — 512 MB, ARMv7 Cortex-A7 | **No model.** Proves the contract: ledger, loop, memory layers, at ~2-3 W. |
| Mid | Orange Pi 4 Pro — 4 GB LPDDR5, Allwinner A733 | **Real models.** The escalation ladder, NVMe, and the section 12.1 measurements. |
| Standard | Raspberry Pi 5 — 8 GB, NVMe hat | **The reproducibility anchor.** Numbers anyone can check. |

Rules that follow:

- **Publish reference numbers from the Pi 5.** Section 10.3 sells determinism and
  audit. A figure measured on a vendor-BSP Allwinner kernel is one a reader has to
  take on trust, which undercuts the claim it is meant to support.
- **WSL2 is for development, never for a published measurement.** Use
  `MachineProfile.detect("wsl2-x86", representative=False)`. The flag changes the
  machine hash, so a desktop number can never be mistaken for an edge number.
- **Run `CostModel.stale_manifests()` after every hardware change.** A manifest
  written on one machine is a guess on the next. Section 12.4.
- The Orange Pi Lite is **not** disqualified as an AI host, only from the
  350M -> 1.2B ladder. A 90-130 MB model on a `background` budget is likely to
  work there, and measuring that is a cheap and interesting experiment.

Buying rule, if a board must be replaced. One spec decides it: the core must
implement **ARMv8.2-A**, which is where the SDOT instruction arrived. It is worth
2-3x on quantized inference.

- Buy: Cortex-**A55**, A75, **A76**, A78, newer.
- Avoid: Cortex-A53, A72, A73, A35. **This rules out the Raspberry Pi 4**, whose
  A72 is ARMv8.0-A — the obvious fallback, and the wrong one.

Then: 64-bit OS, 4 GB, eMMC or NVMe rather than microSD only, and a SoC with real
mainline support. Rockchip is reliable, Amlogic is adequate, Allwinner is where
the risk lives.

Treat an NPU as if it is not there. llama.cpp has no backend for the Vivante or
Rockchip NPUs, their userspace drivers are proprietary and non-redistributable
(so P9 makes them awkward to provision), and decode is bandwidth-bound anyway, so
TOPS does not move the number that matters.

## Conventions

- Python 3.11 is the declared floor; 3.14 is what development runs on. CI covers both.
- `ruff check .` and `pytest -q` must both pass. `RUF022` is ignored on purpose:
  `__all__` is grouped by meaning, and the grouping is the documentation.
- Documentation prose is **ASD-STE100 Simplified Technical English**: short
  sentences, one idea each, no idiom. Match the existing tone in `docs/`.
- Comments in this codebase explain *why the rule exists*, not what the line does.
  Match that density; do not add narration.

## Commands

```bash
.venv/Scripts/python -m pytest -q          # 421 pass, 6 skip without a model
.venv/Scripts/python -m ruff check .
pico-failures list                          # the operator reports
pico-failures report --name demotion_candidates --floor 0.8
```
