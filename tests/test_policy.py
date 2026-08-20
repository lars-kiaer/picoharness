"""The two filters of section 6.4 that read the ledger, and the loop they close.

    manifest cost  ->  selection policy (6.4)  ->  call  ->  ledger
          ^                                                    |
          +---------  v_provider_cost  <----  ingest  <--------+

Section 12.4 draws that diagram and calls the loop half built. The half that
was missing was not a subsystem: the durations were already recorded and the
views already aggregated them, but nothing read the result back into a
decision. These tests are the other half.

Two rules are tested harder than the filters themselves, because both protect a
provider from a number it has not earned. Below the sample floor there is no
measurement, only a call count, and neither filter may act on it. And a replay
uses the numbers the ledger recorded, never the numbers in the database now.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from picoharness.adapters import CodeAdapter
from picoharness.memory import EpisodicIndex, read_measurements
from picoharness.memory.samples import write_ledgers
from picoharness.policy import Measure, Snapshot
from picoharness.registry import Capability, CapabilityGap, Registry
from tests.test_runtime import build, plan_for


class StubGguf:
    """Enough of an adapter for `select()` to consider a model provider.

    `select()` reads the kind and nothing else. A real `gguf` adapter is v2 and
    needs a Linux box; the ordering rule it must obey can be tested now.
    """

    kind = "gguf"

    def load(self, manifest: dict[str, Any]) -> Any:  # pragma: no cover - never called
        raise NotImplementedError

    def run(self, handle: Any, payload: Any, schema_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def unload(self, handle: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def probe(self, manifest: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


def a_manifest(provider_id: str, kind: str = "code") -> dict[str, Any]:
    return {
        "id": provider_id,
        "implements": ["extract@1"],
        "kind": kind,
        "entrypoint": "picoharness.providers.log_summary:extract",
        "produces": ["log_summary@2"],
        "modality_in": ["text"],
        "security": {"max_trust_in": "T1", "may_emit_control": False},
        "determinism": "exact",
    }


def a_registry(*provider_ids: str, kinds: dict[str, str] | None = None) -> Registry:
    kinds = kinds or {}
    registry = Registry()
    registry.add_adapter(CodeAdapter())
    registry.add_adapter(StubGguf())
    registry.add_capability(Capability("extract@1", "text", "log_summary@2"))
    for provider_id in provider_ids:
        registry.add_provider(a_manifest(provider_id, kinds.get(provider_id, "code")))
    return registry


def snapshot(rows: dict[str, Measure]) -> Snapshot:
    """Measurements by provider, pooled over every schema."""
    return Snapshot({(provider_id, None): row for provider_id, row in rows.items()})


def chosen(registry: Registry, **kwargs: Any) -> str:
    return registry.select("extract@1", schema="log_summary@2", **kwargs).id


# --------------------------------------------------------------------------
# the quality floor, sections 6.4 and 10.5
# --------------------------------------------------------------------------


def test_a_provider_below_the_floor_is_not_selected() -> None:
    registry = a_registry("a-weak", "b-sound")
    measured = snapshot({"a-weak": Measure(calls=20, pass_rate=0.55)})
    assert chosen(registry, measured=measured, quality_floor=0.8) == "b-sound"


def test_too_few_calls_cannot_demote_a_provider() -> None:
    """Section 12.4. One failure is not a bad provider, so there is no number."""
    registry = a_registry("a-weak", "b-sound")
    measured = snapshot({"a-weak": Measure(calls=3)})
    assert chosen(registry, measured=measured, quality_floor=0.8) == "a-weak"


def test_no_floor_means_no_quality_filter() -> None:
    registry = a_registry("a-weak", "b-sound")
    measured = snapshot({"a-weak": Measure(calls=40, pass_rate=0.1)})
    assert chosen(registry, measured=measured) == "a-weak"


def test_the_last_provider_below_the_floor_is_a_capability_gap() -> None:
    """A gap is a valid outcome. Section 6.6: it must say what it wanted."""
    registry = a_registry("a-weak")
    measured = snapshot({"a-weak": Measure(calls=40, pass_rate=0.2)})
    with pytest.raises(CapabilityGap, match=r"pass rate of 0\.8"):
        chosen(registry, measured=measured, quality_floor=0.8)


# --------------------------------------------------------------------------
# cost against the remaining budget, section 12.4
# --------------------------------------------------------------------------


def test_a_provider_that_cannot_finish_in_the_budget_is_not_selected() -> None:
    registry = a_registry("a-slow", "b-quick")
    measured = snapshot(
        {"a-slow": Measure(calls=30, p90_ms=900.0), "b-quick": Measure(calls=30, p90_ms=20.0)}
    )
    assert chosen(registry, measured=measured, budget_ms=500) == "b-quick"


def test_an_unmeasured_provider_is_never_excluded_on_cost() -> None:
    """Filtering on a number nobody has is filtering on a guess."""
    registry = a_registry("a-slow", "b-unknown")
    measured = snapshot({"a-slow": Measure(calls=30, p90_ms=900.0)})
    assert chosen(registry, measured=measured, budget_ms=500) == "b-unknown"


def test_a_budget_that_fits_nobody_is_a_capability_gap() -> None:
    registry = a_registry("a-slow")
    measured = snapshot({"a-slow": Measure(calls=30, p90_ms=900.0)})
    with pytest.raises(CapabilityGap, match="500 ms of budget"):
        chosen(registry, measured=measured, budget_ms=500)


# --------------------------------------------------------------------------
# the order, section 6.4: code first, then cheapest
# --------------------------------------------------------------------------


def test_the_cheaper_provider_wins_within_a_kind() -> None:
    """Cost decides before the name does, or the sort is alphabetical theatre."""
    registry = a_registry("a-dear", "b-cheap")
    measured = snapshot(
        {"a-dear": Measure(calls=30, p90_ms=400.0), "b-cheap": Measure(calls=30, p90_ms=9.0)}
    )
    assert chosen(registry, measured=measured) == "b-cheap"


def test_code_still_beats_a_cheaper_model() -> None:
    """The rule that makes the system faster every time a parser replaces a model."""
    registry = a_registry("a-parser", "b-model", kinds={"b-model": "gguf"})
    measured = snapshot(
        {"a-parser": Measure(calls=30, p90_ms=50.0), "b-model": Measure(calls=30, p90_ms=5.0)}
    )
    assert chosen(registry, measured=measured) == "a-parser"


def test_an_unmeasured_provider_sorts_after_a_measured_one() -> None:
    """A manifest with no cost is one nobody probed. Section 6.3 says so."""
    registry = a_registry("a-unknown", "b-measured")
    measured = snapshot({"b-measured": Measure(calls=30, p90_ms=300.0)})
    assert chosen(registry, measured=measured) == "b-measured"


def test_with_nothing_measured_the_order_is_unchanged() -> None:
    """v1 behaviour survives: no measurements, no new decisions."""
    registry = a_registry("a-first", "b-second")
    assert chosen(registry) == "a-first"
    assert chosen(registry, measured=Snapshot(), quality_floor=0.9, budget_ms=1) == "a-first"


# --------------------------------------------------------------------------
# the snapshot itself
# --------------------------------------------------------------------------


def test_a_snapshot_survives_a_round_trip_through_the_ledger() -> None:
    original = Snapshot({("p", "log_summary@2"): Measure(calls=7, p90_ms=12.5, pass_rate=0.86)})
    again = Snapshot.from_json(original.to_json())
    assert again.of("p", "log_summary@2") == original.of("p", "log_summary@2")


def test_the_pooled_row_answers_for_a_schema_nobody_measured() -> None:
    measured = Snapshot({("p", None): Measure(calls=9, p90_ms=30.0)})
    assert measured.of("p", "a_new_schema@1").p90_ms == 30.0


def test_an_unknown_provider_measures_as_nothing() -> None:
    assert Snapshot().of("nobody").calls == 0
    assert Snapshot().of("nobody").p90_ms is None


# --------------------------------------------------------------------------
# where the numbers come from, sections 9.7 and 10.5
# --------------------------------------------------------------------------


def test_a_small_sample_reports_its_count_and_no_numbers(tmp_path: Path) -> None:
    """The sample ledgers hold two or three calls each. That is not a trend."""
    write_ledgers(tmp_path / "sessions")
    index = EpisodicIndex(tmp_path / "episodic.db")
    index.ingest_dir(tmp_path / "sessions")
    measured = read_measurements(index.conn)
    index.close()

    row = measured.of("extract-350m-q4", "build_summary@1")
    assert row.calls > 0, "the provider was seen"
    assert row.p90_ms is None and row.pass_rate is None, "but not often enough to say anything"


def test_the_numbers_come_from_running_the_system(tmp_path: Path) -> None:
    """No fixture and no second counter: three real tasks, then a query."""
    for run in range(3):
        runtime, data = build(tmp_path, session=f"job-{run}")
        runtime.run("why is the disk full on host-a", plan_for(data))
        runtime.ledger.close()

    index = EpisodicIndex(tmp_path / "episodic.db")
    index.ingest_dir(tmp_path / "sessions")
    measured = read_measurements(index.conn)
    index.close()

    # Two log steps per run is six calls, which clears the sample floor of five.
    logs = measured.of("code-logsummary", "log_summary@2")
    assert logs.calls == 6
    assert logs.p90_ms is not None and logs.p90_ms >= 0.0
    assert logs.pass_rate == 1.0
    # One disk step per run is three calls, which does not.
    assert measured.of("code-dfparse", "disk_usage@1").p90_ms is None


# --------------------------------------------------------------------------
# the runtime end of it
# --------------------------------------------------------------------------


def test_the_runtime_records_what_the_policy_measured(tmp_path: Path) -> None:
    """Section 10.3. A replay must not have to trust today's database."""
    measured = snapshot({"code-logsummary": Measure(calls=30, p90_ms=8.0, pass_rate=0.99)})
    runtime, data = build(tmp_path, measured=measured, quality_floor=0.8)
    runtime.run("goal", plan_for(data))
    runtime.ledger.close()

    events = [e for e in runtime.ledger.events() if e["type"] == "policy_snapshot"]
    assert len(events) == 1
    assert events[0]["quality_floor"] == 0.8
    replayed = Snapshot.from_json(events[0]["measurements"])
    assert replayed.of("code-logsummary").p90_ms == 8.0

    # A new event type must not disturb ingest. The memory layers skip what they
    # do not index, and the three facts must still arrive.
    index = EpisodicIndex(tmp_path / "episodic.db")
    index.ingest_dir(tmp_path / "sessions")
    assert index.stats()["facts"] == 3
    index.close()


def test_a_run_with_nothing_measured_writes_no_snapshot(tmp_path: Path) -> None:
    """Silence costs nothing. A v1 ledger keeps the shape it had."""
    runtime, data = build(tmp_path)
    runtime.run("goal", plan_for(data))
    runtime.ledger.close()
    assert not [e for e in runtime.ledger.events() if e["type"] == "policy_snapshot"]


def test_a_demoted_provider_declines_rather_than_answering_badly(tmp_path: Path) -> None:
    """Section 10.5 demotes; with nothing to demote to, section 6.6 declines.

    The step that has a sound provider still commits its fact. That is the whole
    argument of section 5.2: a failing step does not end the task.
    """
    measured = snapshot({"code-logsummary": Measure(calls=40, pass_rate=0.31)})
    runtime, data = build(tmp_path, measured=measured, quality_floor=0.8)
    outcome = runtime.run("why is the disk full on host-a", plan_for(data))
    runtime.ledger.close()

    gaps = [e for e in runtime.ledger.events() if e["type"] == "capability_gap"]
    assert len(gaps) == 2, "both log steps found no provider they were allowed to use"
    assert "pass rate of 0.8" in gaps[0]["reason"]
    assert outcome.missing == ("s2", "s3")
    assert len(outcome.facts) == 1, "the disk step was never in question"
