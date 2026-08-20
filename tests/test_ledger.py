"""Tests for the ledger, the payload, and the trust vocabulary.

The one that matters most is `test_written_ledger_ingests_cleanly`. The memory
layers were built first and they read a format that nothing produced. This test
is the contract between the two halves: if the writer drifts, it fails here and
not six months later in a report that is quietly empty.
"""

from __future__ import annotations

import json

import pytest

from picoharness.ledger import (
    EVENT_TYPES,
    Ledger,
    LedgerError,
    ProviderInput,
    ReplayClock,
    VisibilityViolation,
    assert_visible,
    project,
    read_events,
)
from picoharness.memory import EpisodicIndex
from picoharness.memory.failure import EVENT_TO_KIND
from picoharness.payload import Payload, text
from picoharness.trust import TrustError, may_control, worst

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def fixed_clock(times: list[str]):
    """A clock that hands out a known list, so a test can assert on `t`."""
    it = iter(times)
    return lambda: next(it)


def three_step_session(session_dir, clock=None) -> Ledger:
    """One realistic session: a clean step, a failed step, then a recovery.

    Deliberately the same shape as the samples in `memory/samples.py`, because
    that module is the format's executable specification.
    """
    led = Ledger(session_dir, session_id="job-9001", clock=clock or fixed_clock(
        [f"2026-08-19T10:00:{i:02d}Z" for i in range(20)]
    ))
    led.append("composition", hash="sha256:41d0", machine="sha256:7b2e")
    led.append("user_input", text="why is the disk full on host-a")
    led.append("plan_created", steps=["s1", "s2"])

    led.append("step_started", step="s1", tool="get_disk_usage", trust="T2", subject="host-a")
    led.append("tool_output", step="s1", text="/dev/sda1 88% /", mime="text/plain")
    led.append(
        "fact_added",
        step="s1",
        provider="code-dfparse",
        schema="disk_usage@1",
        duration_ms=3.2,
        fact={"mount": "/dev/sda1", "disk_free_pct": 12, "state": "critical"},
        valid_until="2099-01-01T00:00:00Z",
    )

    led.append("step_started", step="s2", tool="read_syslog", trust="T1", subject="host-a")
    led.append(
        "validation_failed",
        step="s2",
        capability="extract@1",
        tool="read_syslog",
        provider="extract-350m-q4",
        schema="log_summary@2",
        attempt=1,
        duration_ms=610.0,
        error="error_count missing",
    )
    led.append(
        "fact_added",
        step="s2",
        provider="extract-1.2b-q4",
        schema="log_summary@2",
        duration_ms=2140.0,
        fact={"error_count": 7, "first_error": "disk I/O timeout"},
    )
    led.append("answer_sent", outcome="answered")
    return led


# --------------------------------------------------------------------------
# the contract with the memory layers
# --------------------------------------------------------------------------


def test_event_vocabulary_covers_the_memory_layers() -> None:
    """A type the store understands but the writer cannot emit is dead code.

    The reverse is worse: a type the writer emits and the store ignores makes a
    ledger that indexes to nothing, with no error anywhere.
    """
    missing = set(EVENT_TO_KIND) - EVENT_TYPES
    assert not missing, f"the writer cannot emit failure types the store reads: {missing}"

    spine = {"composition", "user_input", "step_started", "fact_added",
             "answer_sent", "declined", "plan_created"}
    assert spine <= EVENT_TYPES


def test_written_ledger_ingests_cleanly(tmp_path) -> None:
    """The whole point of building the runtime: real ledgers, real reports."""
    with three_step_session(tmp_path / "sessions" / "job-9001"):
        pass

    ix = EpisodicIndex(tmp_path / "memory" / "episodic.db")
    found = ix.ingest_dir(tmp_path / "sessions")
    assert found == ["job-9001"]

    stats = ix.stats()
    assert stats["episodes"] == 1
    assert stats["facts"] == 2
    assert stats["failures"] == 1

    episode = ix.conn.execute("SELECT * FROM episode").fetchone()
    assert episode["goal"] == "why is the disk full on host-a"
    assert episode["outcome"] == "answered"
    assert episode["composition_hash"] == "sha256:41d0"
    ix.close()


def test_trust_survives_the_round_trip(tmp_path) -> None:
    """A fact from a T1 tool must still be T1 after it has been stored."""
    with three_step_session(tmp_path / "sessions" / "job-9001"):
        pass
    ix = EpisodicIndex(tmp_path / "memory" / "episodic.db")
    ix.ingest_dir(tmp_path / "sessions")

    rows = {r["schema_id"]: r["trust"] for r in ix.conn.execute("SELECT * FROM fact")}
    assert rows["disk_usage@1"] == "T2"      # the tool declared T2
    assert rows["log_summary@2"] == "T1"     # a syslog reader declared T1
    ix.close()


def test_durations_reach_the_cost_model(tmp_path) -> None:
    """Without `duration_ms` the cost model has nothing to measure."""
    with three_step_session(tmp_path / "sessions" / "job-9001"):
        pass
    ix = EpisodicIndex(tmp_path / "memory" / "episodic.db")
    ix.ingest_dir(tmp_path / "sessions")

    rows = ix.conn.execute(
        "SELECT provider_id, calls FROM v_provider_cost ORDER BY provider_id"
    ).fetchall()
    seen = {r["provider_id"] for r in rows}
    # The failed call cost time too, so it is counted. See 12.4.
    assert "extract-350m-q4" in seen
    assert "extract-1.2b-q4" in seen
    ix.close()


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def test_event_zero_must_be_composition(tmp_path) -> None:
    with Ledger(tmp_path / "s") as led:
        with pytest.raises(LedgerError, match="event 0 must be"):
            led.append("user_input", text="hello")


def test_unknown_event_type_is_refused(tmp_path) -> None:
    with Ledger(tmp_path / "s") as led:
        led.append("composition", hash="sha256:0")
        with pytest.raises(LedgerError, match="unknown event type"):
            led.append("fact_addded", fact={})  # a typo, on purpose


def test_one_line_per_event_and_seq_is_monotonic(tmp_path) -> None:
    with Ledger(tmp_path / "s") as led:
        led.append("composition", hash="sha256:0")
        for i in range(5):
            led.append("step_started", step=f"s{i}", tool="t", trust="T1")

    lines = (tmp_path / "s" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    assert [json.loads(ln)["seq"] for ln in lines] == list(range(6))


def test_resumes_after_a_crash(tmp_path) -> None:
    """All state is on disk, so continuing is a read and not a recovery."""
    led = Ledger(tmp_path / "s")
    led.append("composition", hash="sha256:0")
    led.append("user_input", text="first half")
    led.close()  # stand-in for the power going off

    again = Ledger(tmp_path / "s")
    assert again.next_seq == 2
    again.append("answer_sent", outcome="answered")
    again.close()

    assert [e["seq"] for e in read_events(tmp_path / "s" / "events.jsonl")] == [0, 1, 2]


def test_none_fields_are_not_written(tmp_path) -> None:
    """An absent value is absent. A null in the ledger is a value someone chose."""
    with Ledger(tmp_path / "s") as led:
        led.append("composition", hash="sha256:0", machine=None)
    event = next(iter(read_events(tmp_path / "s" / "events.jsonl")))
    assert "machine" not in event


def test_blob_round_trip(tmp_path) -> None:
    with Ledger(tmp_path / "s") as led:
        led.append("composition", hash="sha256:0")
        ref = led.write_blob("s1.raw", "a" * 4096)
        led.append("tool_output", step="s1", blob=ref, bytes=4096)
        assert led.read_blob(ref) == b"a" * 4096
        assert ref == "blobs/s1.raw"


# --------------------------------------------------------------------------
# deterministic replay, section 10.3
# --------------------------------------------------------------------------


def test_replay_clock_reproduces_the_original_bit_for_bit(tmp_path) -> None:
    """Time is an input to the run. A replay feeds the recorded times back in."""
    original_dir = tmp_path / "sessions" / "job-9001"
    with three_step_session(original_dir):
        pass
    original = (original_dir / "events.jsonl").read_text(encoding="utf-8")

    replay_dir = tmp_path / "replay" / "job-9001"
    clock = ReplayClock(read_events(original_dir / "events.jsonl"))
    with three_step_session(replay_dir, clock=clock):
        pass
    assert (replay_dir / "events.jsonl").read_text(encoding="utf-8") == original


def test_replay_clock_reports_divergence(tmp_path) -> None:
    """More events than the original is a divergence, not a detail."""
    clock = ReplayClock([{"t": "2026-08-19T10:00:00Z"}])
    with Ledger(tmp_path / "s", clock=clock) as led:
        led.append("composition", hash="sha256:0")
        with pytest.raises(LedgerError, match="diverged"):
            led.append("user_input", text="one event too many")


# --------------------------------------------------------------------------
# the visibility invariant, section 4.5
# --------------------------------------------------------------------------


def test_project_rebuilds_the_input_from_the_ledger_alone(tmp_path) -> None:
    with three_step_session(tmp_path / "s") as led:
        events = led.events()

    rebuilt = project(events, step="s1", capability="extract@1", schema_id="disk_usage@1")
    assert rebuilt.payload.as_text() == "/dev/sda1 88% /"
    assert rebuilt.payload.trust == "T2"


def test_project_leaves_out_facts_above_the_trust_ceiling(tmp_path) -> None:
    """A provider that may not read T1 must not be handed a T1 fact.

    Filtering here, and not later, means the input the runtime compares against
    is the input the provider actually receives.
    """
    with three_step_session(tmp_path / "s") as led:
        events = led.events()

    open_input = project(events, step="s3", capability="plan@1", schema_id="plan@1")
    strict = project(
        events, step="s3", capability="plan@1", schema_id="plan@1", max_trust_in="T2"
    )
    assert len(open_input.facts) == 2
    assert len(strict.facts) == 1
    assert all(trust == "T2" for _, trust in strict.facts)


def test_assert_visible_catches_an_unlogged_field(tmp_path) -> None:
    """The bug this exists for: a prompt grows a field that nobody wrote down."""
    with three_step_session(tmp_path / "s") as led:
        events = led.events()

    rebuilt = project(events, step="s1", capability="extract@1", schema_id="disk_usage@1")
    assert_visible(rebuilt, rebuilt)  # the honest case passes

    smuggled = ProviderInput(
        step="s1",
        capability="extract@1",
        schema_id="disk_usage@1",
        payload=text("/dev/sda1 88% /  AND ALSO the operator hint", trust="T2"),
    )
    with pytest.raises(VisibilityViolation):
        assert_visible(smuggled, rebuilt)


def test_provider_input_trust_is_the_worst_of_its_parts() -> None:
    pi = ProviderInput(
        step="s1",
        capability="plan@1",
        schema_id="plan@1",
        payload=text("goal", trust="T0"),
        facts=(("{}", "T2"), ("{}", "T1")),
    )
    assert pi.trust == "T1"


# --------------------------------------------------------------------------
# payload and trust
# --------------------------------------------------------------------------


def test_payload_carries_bytes_as_well_as_text() -> None:
    """The pipe is wide enough for a scanned page before one exists."""
    blob = Payload(data=b"\x89PNG\r\n", mime="image/png", trust="T1")
    assert not blob.is_text
    assert blob.nbytes == 6


def test_payload_refuses_to_guess_at_bad_bytes() -> None:
    """A mangled character becomes a wrong value three steps later."""
    blob = Payload(data=b"\xff\xfe\x00", mime="application/octet-stream")
    with pytest.raises(ValueError, match="not utf-8 text"):
        blob.as_text()


def test_truncation_leaves_a_trace() -> None:
    """A cut with no trace is a cut that gets blamed on the provider."""
    cut = text("x" * 100).truncated(10)
    assert cut.nbytes == 10
    assert cut.meta["truncated_from"] == 100
    assert text("short").truncated(10).meta == {}


def test_trust_is_inherited_never_upgraded() -> None:
    assert worst(["T2", "T2"]) == "T2"
    assert worst(["T2", "T1"]) == "T1"
    assert worst(["T0", "T2"]) == "T0"
    assert worst([]) == "T2"


def test_control_plane_rejects_outside_data() -> None:
    assert may_control("T0") and may_control("T2")
    assert not may_control("T1")


def test_an_unknown_trust_level_is_a_typo_not_a_default() -> None:
    with pytest.raises(TrustError):
        worst(["T3"])
    with pytest.raises(TrustError):
        Payload(data="x", trust="trusted")  # type: ignore[arg-type]
