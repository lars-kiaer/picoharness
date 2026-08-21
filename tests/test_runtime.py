"""The v1 exit test, and the generality tests of section 17.

Section 15 states what v1 has to prove:

> A three-step task runs end to end and replays identically. No model is
> involved.

The second sentence carries as much weight as the first. A system that works
with only a parser proves that the ledger, the loop and the validation are
correct; add a model first and you will not know which layer is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from picoharness.adapters import CodeAdapter
from picoharness.budget import Breakers, Budget, Limit
from picoharness.composition import Composition, MachineProfile, boot
from picoharness.hooks import Hooks, size_limit
from picoharness.ledger import Ledger, ReplayClock, first_difference, read_events
from picoharness.memory import EpisodicIndex
from picoharness.registry import Capability, CapabilityGap, Registry
from picoharness.runtime import Runtime, Step
from picoharness.schemas import READ_PATH_SCHEMA
from picoharness.tools import read_disk, read_log
from picoharness.validate import Schema
from picoharness.world import LocalWorld

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

DF_CAPTURE = """\
Filesystem     1K-blocks      Used Available Use% Mounted on
/dev/sda1       41152000  36214000   2838000  93% /
/dev/sdb1      103880000  20776000  77808000  21% /srv
"""

MANIFESTS = [
    {
        "id": "code-logsummary",
        "implements": ["extract@1"],
        "kind": "code",
        "entrypoint": "picoharness.providers.log_summary:extract",
        "produces": ["log_summary@2"],
        "modality_in": ["text"],
        "security": {"max_trust_in": "T1", "may_emit_control": False},
        "determinism": "exact",
    },
    {
        "id": "code-diskusage",
        "implements": ["extract@1"],
        "kind": "code",
        "entrypoint": "picoharness.providers.disk_usage:extract",
        "produces": ["disk_usage@1"],
        "modality_in": ["text"],
        "security": {"max_trust_in": "T2", "may_emit_control": False},
        "determinism": "exact",
    },
    {
        "id": "code-answer",
        "implements": ["answer@1"],
        "kind": "code",
        "entrypoint": "picoharness.providers.answer:render",
        "modality_in": ["text", "application"],
        "security": {"max_trust_in": "T2", "may_emit_control": False},
        "determinism": "exact",
    },
]

# `disk_usage@1` is declared here rather than in a file. A schema registry on
# disk is a v3 concern; v1 needs one schema for one tool, and inventing a
# directory layout for it now would be guessing at section 16 question 7.
DISK_SCHEMA = Schema(
    schema_id="disk_usage@1",
    body={
        "type": "object",
        "required": ["mount", "disk_free_pct", "state"],
        "properties": {
            "mount": {"type": ["string", "null"]},
            "mounted_on": {"type": ["string", "null"]},
            "disk_free_pct": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
            "state": {"type": ["string", "null"], "enum": ["ok", "low", "critical", None]},
        },
    },
)


# --------------------------------------------------------------------------
# a whole system, assembled
# --------------------------------------------------------------------------


def build(tmp_path: Path, *, manifests=None, budget=None, session="job-v1",
          clock=None, data: Path | None = None, adapters=(), **policy):
    """Everything a task needs, wired the way a real boot would wire it.

    `policy` carries the two arguments of section 6.4 that need measurements,
    `measured` and `quality_floor`. `tests/test_policy.py` is their caller.
    `adapters` adds a second kind beside `code`; `tests/test_gguf.py` uses it
    for the swap test, and passing the same call two manifest lists is what
    makes that test mean anything.
    """
    data = data or (tmp_path / "data")
    data.mkdir(parents=True, exist_ok=True)
    (data / "df.txt").write_text(DF_CAPTURE, encoding="utf-8")
    (data / "syslog").write_text(
        (FIXTURES / "extract" / "normal-01-syslog-disk-io.input").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (data / "build.log").write_text(
        (FIXTURES / "extract" / "normal-06-systemd-restart-loop.input").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    registry = Registry()
    registry.add_adapter(CodeAdapter())
    for adapter in adapters:
        registry.add_adapter(adapter)
    registry.add_capability(Capability("extract@1", "text", "schema", is_control=False))
    registry.add_capability(Capability("answer@1", "facts", "text", is_control=False))
    for manifest in manifests if manifests is not None else MANIFESTS:
        registry.add_provider(manifest)

    ledger = Ledger(tmp_path / "sessions" / session, session_id=session, clock=clock or None) \
        if clock else Ledger(tmp_path / "sessions" / session, session_id=session)

    world = LocalWorld.rooted_at(data)
    hooks = Hooks().on("on_output", size_limit(1 << 16))
    schemas = {
        "log_summary@2": Schema.from_file(FIXTURES / "schemas" / "log_summary@2.json"),
        "disk_usage@1": DISK_SCHEMA,
        # A tool that declares an input schema needs that schema registered.
        # The runtime refuses to run the tool otherwise, rather than passing the
        # arguments through unchecked.
        "read_path@1": Schema(schema_id="read_path@1", body=READ_PATH_SCHEMA),
    }
    runtime = Runtime(
        ledger,
        registry,
        world,
        tools={"read_log": read_log(), "read_disk": read_disk()},
        schemas=schemas,
        budget=budget or Budget("attended"),
        hooks=hooks,
        **policy,
    )
    boot(
        ledger,
        Composition(
            manifests={m["id"]: m for m in (manifests if manifests is not None else MANIFESTS)},
            tools={n: t.resolve() for n, t in runtime.tools.items()},
            policy={"hooks": hooks.resolve(), "world": world.resolve()},
        ),
        MachineProfile.detect("test-box", representative=False),
    )
    return runtime, data


PLAN = [
    Step("s1", "read_disk", {"path": "df.txt"}, subject="host-a"),
    Step("s2", "read_log", {"path": "syslog"}, subject="host-a"),
    Step("s3", "read_log", {"path": "build.log"}, subject="host-a"),
]


def plan_for(data: Path) -> list[Step]:
    return [
        Step(s.id, s.tool, {"path": str(data / s.args["path"])}, subject=s.subject)
        for s in PLAN
    ]


# --------------------------------------------------------------------------
# the v1 exit test
# --------------------------------------------------------------------------


def test_a_three_step_task_runs_end_to_end(tmp_path: Path) -> None:
    runtime, data = build(tmp_path)
    outcome = runtime.run("why is the disk full on host-a", plan_for(data))
    runtime.ledger.close()

    assert outcome.ok, outcome
    assert len(outcome.facts) == 3
    assert outcome.missing == ()
    assert "disk_free_pct=7" in outcome.answer


def test_no_model_was_involved(tmp_path: Path) -> None:
    """v1 must have no model. Every provider that ran is a parser."""
    runtime, data = build(tmp_path)
    runtime.run("goal", plan_for(data))
    runtime.ledger.close()

    used = {e["provider"] for e in runtime.ledger.events() if e.get("provider")}
    assert used <= {"code-logsummary", "code-diskusage", "code-answer"}
    for provider_id in used:
        assert runtime.registry.providers[provider_id].kind == "code"


def test_the_ledger_ingests_into_the_memory_layers(tmp_path: Path) -> None:
    """The whole reason the runtime is built before the model work."""
    runtime, data = build(tmp_path)
    runtime.run("why is the disk full on host-a", plan_for(data))
    runtime.ledger.close()

    ix = EpisodicIndex(tmp_path / "memory" / "episodic.db")
    assert ix.ingest_dir(tmp_path / "sessions") == ["job-v1"]
    stats = ix.stats()
    assert stats["episodes"] == 1 and stats["facts"] == 3

    hit = ix.field_range("disk_free_pct", below=15)
    assert hit and hit[0].payload["mount"] == "/dev/sda1"
    ix.close()


def test_replays_identically(tmp_path: Path) -> None:
    """Deterministic replay, not transcript replay. Section 10.3."""
    first, data = build(tmp_path, session="job-v1")
    first.run("why is the disk full on host-a", plan_for(data))
    first.ledger.close()
    original = (tmp_path / "sessions" / "job-v1" / "events.jsonl").read_text(encoding="utf-8")

    # The same data root, because the world is part of the composition and a
    # replay against a different one is a different system, not a replay.
    replay_dir = tmp_path / "again"
    replay_dir.mkdir()
    clock = ReplayClock(read_events(tmp_path / "sessions" / "job-v1" / "events.jsonl"))
    second, data2 = build(replay_dir, session="job-v1", clock=clock, data=data)
    second.run("why is the disk full on host-a", plan_for(data2))
    second.ledger.close()

    replayed = (replay_dir / "sessions" / "job-v1" / "events.jsonl").read_text(encoding="utf-8")
    difference = first_difference(
        [json.loads(ln) for ln in original.splitlines()],
        [json.loads(ln) for ln in replayed.splitlines()],
    )
    assert difference is None, difference


def test_replay_reproduces_results_and_not_measurements(tmp_path: Path) -> None:
    """The distinction section 10.3 rests on, made explicit.

    Every field of the replayed ledger matches except the measured ones. A
    duration is an observation of a machine under whatever load it was under; it
    never repeats, and a claim that it does would be untestable. The durations
    stay in the ledger because section 12.4 turns them into the cost model.
    """
    first, data = build(tmp_path, session="job-v1")
    first.run("goal", plan_for(data))
    first.ledger.close()
    original = first.ledger.events()

    replay_dir = tmp_path / "again"
    replay_dir.mkdir()
    clock = ReplayClock(original)
    second, data2 = build(replay_dir, session="job-v1", clock=clock, data=data)
    second.run("goal", plan_for(data2))
    second.ledger.close()
    replayed = second.ledger.events()

    assert first_difference(original, replayed) is None
    # And the durations really were different, or this test proves nothing.
    measured = [
        (a.get("duration_ms"), b.get("duration_ms"))
        for a, b in zip(original, replayed, strict=True)
        if a.get("duration_ms") is not None
    ]
    assert measured, "no call recorded a duration; the cost model would be empty"


# --------------------------------------------------------------------------
# trust, sections 11.2 and 9.5
# --------------------------------------------------------------------------


def test_a_fact_inherits_the_trust_of_its_tool(tmp_path: Path) -> None:
    runtime, data = build(tmp_path)
    runtime.run("goal", plan_for(data))
    runtime.ledger.close()

    ix = EpisodicIndex(tmp_path / "memory" / "episodic.db")
    ix.ingest_dir(tmp_path / "sessions")
    trust = {r["schema_id"]: r["trust"] for r in ix.conn.execute("SELECT * FROM fact")}
    assert trust["disk_usage@1"] == "T2"   # the system read its own disk
    assert trust["log_summary@2"] == "T1"  # a log can hold what an attacker wrote

    # `recall` serves anyone; `recall_for_control` serves the planner. The T1
    # fact is filtered out in SQL, so nothing leaks and nothing raises —
    # `TrustViolation` guards the case where that filter is broken later.
    from picoharness.memory import RecallPolicy

    everyone = ix.recall("", policy=RecallPolicy(limit=20), schema_id="log_summary@2")
    planner = ix.recall_for_control("", schema_id="log_summary@2")
    assert everyone and all(r.trust == "T1" for r in everyone)
    assert planner == []
    ix.close()


def test_a_t2_only_provider_never_sees_t1(tmp_path: Path) -> None:
    """Section 11.2 in one place: the filter is in `select`, before the call."""
    runtime, _ = build(tmp_path)
    with pytest.raises(CapabilityGap):
        runtime.registry.select("extract@1", schema="disk_usage@1", trust_in="T1")
    assert runtime.registry.select("extract@1", schema="disk_usage@1", trust_in="T2")
    runtime.ledger.close()


# --------------------------------------------------------------------------
# the generality tests of section 17
# --------------------------------------------------------------------------


def test_the_delete_test(tmp_path: Path) -> None:
    """Remove a capability. The system must decline cleanly and invent nothing."""
    without_logs = [m for m in MANIFESTS if m["id"] != "code-logsummary"]
    runtime, data = build(tmp_path, manifests=without_logs)
    outcome = runtime.run("why is the disk full on host-a", plan_for(data))
    runtime.ledger.close()

    assert outcome.status == "partial"
    assert outcome.missing == ("s2", "s3")
    assert len(outcome.facts) == 1  # the disk step still worked

    gaps = [e for e in runtime.ledger.events() if e["type"] == "capability_gap"]
    assert len(gaps) == 2
    # It must not invent an answer, and it must say what is not there.
    assert "s2" in outcome.answer and "Incomplete" in outcome.answer


def test_the_swap_test_is_a_manifest_edit(tmp_path: Path) -> None:
    """Section 17: replace a provider, change nothing else.

    Both providers here are `code`, so this is not yet the real v2 test. What it
    does prove is that the runtime reaches a provider only through the manifest:
    the id, the entry point and the schema all move together, and no runtime
    code names any of them.
    """
    swapped = [dict(m) for m in MANIFESTS]
    for manifest in swapped:
        if manifest["id"] == "code-logsummary":
            manifest["id"] = "code-logsummary-v2"

    runtime, data = build(tmp_path, manifests=swapped)
    outcome = runtime.run("goal", plan_for(data))
    runtime.ledger.close()

    assert outcome.ok
    used = {e["provider"] for e in runtime.ledger.events() if e.get("provider")}
    assert "code-logsummary-v2" in used and "code-logsummary" not in used


# --------------------------------------------------------------------------
# still answering when things go wrong, section 5.3
# --------------------------------------------------------------------------


def test_a_missing_file_does_not_stop_the_task(tmp_path: Path) -> None:
    runtime, data = build(tmp_path)
    plan = plan_for(data)
    plan[1] = Step("s2", "read_log", {"path": str(data / "not-there")})
    outcome = runtime.run("goal", plan)
    runtime.ledger.close()

    assert outcome.status == "partial"
    assert "s2" in outcome.missing
    assert len(outcome.facts) == 2
    assert any(e["type"] == "tool_failed" for e in runtime.ledger.events())


def test_a_path_outside_the_world_is_refused(tmp_path: Path) -> None:
    """One execution world, section 11.3. A tool cannot opt out."""
    runtime, _ = build(tmp_path)
    outside = tmp_path / "sessions" / "job-v1" / "events.jsonl"
    plan = [Step("s1", "read_log", {"path": str(outside)})]
    outcome = runtime.run("read something you should not", plan)
    runtime.ledger.close()

    assert outcome.missing == ("s1",)
    failed = [e for e in runtime.ledger.events() if e["type"] == "tool_failed"]
    assert "outside the declared roots" in failed[0]["error"]


def test_a_breaker_still_answers(tmp_path: Path) -> None:
    """When a breaker opens the system must still answer. Section 5.3."""
    budget = Budget("attended", breakers=Breakers(max_steps=1))
    runtime, data = build(tmp_path, budget=budget)
    outcome = runtime.run("goal", plan_for(data))
    runtime.ledger.close()

    assert outcome.status == "partial"
    assert outcome.answer is not None
    tripped = [e for e in runtime.ledger.events() if e["type"] == "breaker_tripped"]
    assert tripped and tripped[0]["reason"] == "max_steps"


def test_an_exhausted_budget_still_answers(tmp_path: Path) -> None:
    budget = Budget("attended", limit=Limit(wall_ms=0))
    runtime, data = build(tmp_path, budget=budget)
    outcome = runtime.run("goal", plan_for(data))
    runtime.ledger.close()

    assert outcome.answer is not None
    assert any(e["type"] == "budget_exhausted" for e in runtime.ledger.events())


def test_the_answer_names_what_is_absent(tmp_path: Path) -> None:
    """Abstention has to survive all the way to the user, or it is decoration."""
    runtime, data = build(tmp_path)
    (data / "no-host.log").write_text(
        (FIXTURES / "extract" / "absent-05-app-log-no-host.input").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    outcome = runtime.run("goal", [Step("s1", "read_log", {"path": str(data / "no-host.log")})])
    runtime.ledger.close()

    assert outcome.facts[0]["host"] is None
    assert "not present in the input: host" in outcome.answer


# --------------------------------------------------------------------------
# the ledger is the whole record
# --------------------------------------------------------------------------


def test_every_event_type_written_is_one_the_store_understands(tmp_path: Path) -> None:
    from picoharness.ledger import EVENT_TYPES

    runtime, data = build(tmp_path)
    runtime.run("goal", plan_for(data))
    runtime.ledger.close()
    assert {e["type"] for e in runtime.ledger.events()} <= EVENT_TYPES


def test_raw_output_is_a_blob_and_the_ledger_stays_greppable(tmp_path: Path) -> None:
    runtime, data = build(tmp_path)
    runtime.run("goal", plan_for(data))
    runtime.ledger.close()

    outputs = [e for e in runtime.ledger.events() if e["type"] == "tool_output"]
    assert len(outputs) == 3
    for event in outputs:
        assert event["blob"].startswith("blobs/")
        assert (runtime.ledger.dir / event["blob"]).exists()
        assert "\n" not in json.dumps(event)  # one line per event


# --------------------------------------------------------------------------
# confidence as a pre-commit gate, section 6.5
# --------------------------------------------------------------------------


def unsure_extract(payload):
    """A provider that knows it is guessing. Used only by the tests below."""
    from picoharness.adapters import Reduced
    from picoharness.providers.log_summary import extract

    return Reduced(extract(payload), confidence=0.30, how="test-fixed")


def confident_extract(payload):
    from picoharness.adapters import Reduced
    from picoharness.providers.log_summary import extract

    return Reduced(extract(payload), confidence=0.95, how="test-fixed")


def _with(entrypoint: str, **extra):
    """The manifest set, with the log reducer swapped for a scoring one."""
    out = []
    for m in MANIFESTS:
        if m["id"] == "code-logsummary":
            out.append({**m, "entrypoint": entrypoint, **extra})
        else:
            out.append(dict(m))
    return out


def test_a_low_confidence_result_is_not_committed(tmp_path: Path) -> None:
    """Escalate before the work is used, not after a validation failure."""
    runtime, data = build(
        tmp_path,
        manifests=_with("tests.test_runtime:unsure_extract", confidence_floor=0.7),
    )
    outcome = runtime.run("goal", [Step("s2", "read_log", {"path": str(data / "syslog")})])
    runtime.ledger.close()

    assert outcome.missing == ("s2",)
    refused = [e for e in runtime.ledger.events() if e["type"] == "validation_failed"]
    assert refused and refused[0]["kind"] == "semantic"
    assert "below the declared floor" in refused[0]["error"]
    assert "test-fixed" in refused[0]["error"]  # the source is named


def test_a_confident_result_commits_and_records_the_score(tmp_path: Path) -> None:
    """The score goes into the ledger, so the floor can later be measured
    rather than chosen. Same shape as the cost model of section 12.4."""
    runtime, data = build(
        tmp_path,
        manifests=_with("tests.test_runtime:confident_extract", confidence_floor=0.7),
    )
    outcome = runtime.run("goal", [Step("s2", "read_log", {"path": str(data / "syslog")})])
    runtime.ledger.close()

    assert outcome.ok
    fact = next(e for e in runtime.ledger.events() if e["type"] == "fact_added")
    assert fact["confidence"] == 0.95


def test_a_provider_without_a_score_is_not_gated(tmp_path: Path) -> None:
    """A missing score is not a low score. Gating on absence would demote every
    parser in the system."""
    runtime, data = build(tmp_path, manifests=_with(
        "picoharness.providers.log_summary:extract", confidence_floor=0.99
    ))
    outcome = runtime.run("goal", [Step("s2", "read_log", {"path": str(data / "syslog")})])
    runtime.ledger.close()

    assert outcome.ok
    fact = next(e for e in runtime.ledger.events() if e["type"] == "fact_added")
    assert "confidence" not in fact  # None fields are not written


def test_no_floor_means_no_gate(tmp_path: Path) -> None:
    """A provider may report a score without the manifest acting on it."""
    runtime, data = build(tmp_path, manifests=_with("tests.test_runtime:unsure_extract"))
    outcome = runtime.run("goal", [Step("s2", "read_log", {"path": str(data / "syslog")})])
    runtime.ledger.close()
    assert outcome.ok


def test_a_deployment_floor_applies_to_every_provider(tmp_path: Path) -> None:
    """The manifest floor is provider policy; this is the installation's own."""
    from picoharness.hooks import confidence_floor

    runtime, data = build(tmp_path, manifests=_with("tests.test_runtime:unsure_extract"))
    runtime.hooks.on("on_commit", confidence_floor(0.8))
    outcome = runtime.run("goal", [Step("s2", "read_log", {"path": str(data / "syslog")})])
    runtime.ledger.close()

    assert outcome.missing == ("s2",)
    refused = [e for e in runtime.ledger.events() if e["type"] == "validation_failed"]
    assert refused and "deployment floor" in refused[0]["error"]


# --------------------------------------------------------------------------
# tool arguments, section 8.1
# --------------------------------------------------------------------------


def test_a_bad_argument_never_reaches_the_tool(tmp_path: Path) -> None:
    """The plan is the control plane, and from v4 a model writes it.

    This is the only schema between a planner and executable code, so an
    argument of the wrong shape must stop here and not at the file system.
    """
    runtime, _ = build(tmp_path)
    outcome = runtime.run("goal", [Step("s1", "read_log", {"pathh": "/etc/passwd"})])
    runtime.ledger.close()

    assert outcome.missing == ("s1",)
    refused = [e for e in runtime.ledger.events() if e["type"] == "validation_failed"]
    assert refused and refused[0]["kind"] == "schema"
    assert refused[0]["schema"] == "read_path@1"
    assert "not in the schema" in refused[0]["error"]
    assert not any(e["type"] == "tool_output" for e in runtime.ledger.events())


def test_an_extra_argument_is_refused(tmp_path: Path) -> None:
    """`additionalProperties: false`. A field nobody declared is a field nobody
    checked, and a plan that carries one has gone somewhere unintended."""
    runtime, data = build(tmp_path)
    outcome = runtime.run(
        "goal",
        [Step("s1", "read_log", {"path": str(data / "syslog"), "recurse": True})],
    )
    runtime.ledger.close()
    assert outcome.missing == ("s1",)


def test_an_unregistered_input_schema_is_loud(tmp_path: Path) -> None:
    """Silence here would mean running the tool with unchecked arguments."""
    runtime, data = build(tmp_path)
    runtime.tools["read_log"].input_schema = "never_registered@1"
    outcome = runtime.run("goal", [Step("s1", "read_log", {"path": str(data / "syslog")})])
    runtime.ledger.close()

    assert outcome.missing == ("s1",)
    refused = [e for e in runtime.ledger.events() if e["type"] == "validation_failed"]
    assert "not registered" in refused[0]["error"]


def test_a_valid_argument_passes_through(tmp_path: Path) -> None:
    runtime, data = build(tmp_path)
    outcome = runtime.run("goal", [Step("s1", "read_log", {"path": str(data / "syslog")})])
    runtime.ledger.close()
    assert outcome.ok
