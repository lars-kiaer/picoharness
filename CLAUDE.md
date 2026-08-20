# picoharness — working rules

A serial micro-agent harness for a CPU-only edge box. The state is a file, models
are pure functions over it, and one provider runs at a time.

Full design: `docs/serial-micro-agent-harness.md` (v0.7).
Model and runtime choices: `docs/model-evaluation-and-runtime.md`.
Read the section named in a rule before you argue with the rule.

## Where the project actually is

Implemented: the **memory layers only** — `EpisodicIndex`, `FailureMemory`,
`CostModel`. That is stage **v6** of the build plan in §15. Stages v0–v5 do not
exist yet, so **nothing in this repository writes a ledger**; the memory layers
read hand-written samples in `memory/samples.py`.

The runtime is the next thing to build, in the model-free form §15 insists on.

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

Field requirements worth remembering: `step_started` carries `trust` and
`subject`; `fact_added` carries `provider`, `schema`, `duration_ms`,
`valid_until`. Without `duration_ms` the cost model has nothing to measure.

`memory/samples.py` is the executable specification of the format. Treat it as the
contract.

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
.venv/Scripts/python -m pytest -q          # 58 tests, about 1 second
.venv/Scripts/python -m ruff check .
pico-failures list                          # the operator reports
pico-failures report --name demotion_candidates --floor 0.8
```
