"""Tests for the self-calibrating cost model.

Run: python3 -m pytest tests/test_cost.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from picoharness.memory import CostModel, EpisodicIndex
from picoharness.memory.cost import DEFAULT_MIN_SAMPLES


def _ledger(root: Path, sid: str, calls: list[tuple[str, float, bool]]) -> None:
    """One session, one call per entry: (provider, duration_ms, succeeded)."""
    events: list[dict] = [
        {"seq": 0, "t": "2026-08-01T09:00:00Z", "type": "composition",
         "session_id": sid, "hash": "sha256:aaaa"},
        {"seq": 1, "t": "2026-08-01T09:00:01Z", "type": "user_input",
         "session_id": sid, "text": "extract the build report"},
    ]
    seq = 2
    for i, (provider, ms, ok) in enumerate(calls):
        step = f"s{i}"
        events.append({"seq": seq, "t": "2026-08-01T09:00:02Z", "type": "step_started",
                       "step": step, "tool": "read_buildlog", "trust": "T1"})
        seq += 1
        if ok:
            events.append({"seq": seq, "t": "2026-08-01T09:00:03Z", "type": "fact_added",
                           "step": step, "provider": provider, "schema": "build_summary@1",
                           "duration_ms": ms, "fact": {"failed_targets": i}})
        else:
            events.append({"seq": seq, "t": "2026-08-01T09:00:03Z", "type": "validation_failed",
                           "step": step, "provider": provider, "schema": "build_summary@1",
                           "capability": "extract@1", "duration_ms": ms,
                           "error": "field missing"})
        seq += 1
    path = root / sid / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


@pytest.fixture()
def cm(tmp_path: Path):
    root = tmp_path / "sessions"
    # A fast small provider with a long tail, and a slow steady large one.
    _ledger(root, "a", [("small", 90, True), ("small", 95, True), ("small", 100, True),
                        ("small", 105, False), ("small", 900, True), ("small", 110, True)])
    _ledger(root, "b", [("large", 800, True), ("large", 810, True), ("large", 820, True),
                        ("large", 830, True), ("large", 840, True), ("large", 850, True)])
    _ledger(root, "c", [("rare", 50, True), ("rare", 55, True)])
    ix = EpisodicIndex(tmp_path / "memory" / "episodic.db")
    ix.ingest_dir(root)
    yield CostModel(ix.conn), ix
    ix.close()


# --------------------------------------------------------------------------


def test_durations_reach_both_tables(cm) -> None:
    _model, ix = cm
    facts = ix.conn.execute(
        "SELECT COUNT(*) FROM fact WHERE duration_ms IS NOT NULL").fetchone()[0]
    fails = ix.conn.execute(
        "SELECT COUNT(*) FROM failure WHERE duration_ms IS NOT NULL").fetchone()[0]
    assert facts == 13 and fails == 1


def test_a_small_sample_defers_to_the_manifest(cm) -> None:
    """Two calls is not a trend. The policy must keep its declared number."""
    model, _ = cm
    est = model.estimate("rare", "build_summary@1")
    assert est.calls < DEFAULT_MIN_SAMPLES
    assert est.confidence == "manifest"
    assert est.budget_ms is None
    assert "no measurement yet" in est.line()


def test_enough_calls_produce_an_estimate(cm) -> None:
    model, _ = cm
    est = model.estimate("large", "build_summary@1")
    assert est.calls == 6
    assert est.confidence == "low"          # measured, but under GOOD_SAMPLES
    assert 800 <= est.p50_ms <= 850
    assert est.budget_ms == est.p90_ms


def test_p90_not_the_mean_carries_the_tail(cm) -> None:
    """The mean hides the 900 ms outlier. The budget must not."""
    model, _ = cm
    est = model.estimate("small", "build_summary@1")
    assert est.p50_ms < 200, "the typical call is fast"
    assert est.p90_ms > est.mean_ms, "p90 must sit above the mean here"
    assert est.p90_ms >= 900, "the tail is what breaks a budget"


def test_failed_calls_still_cost_time(cm) -> None:
    """A rejected output consumed the CPU. It belongs in the estimate."""
    model, _ = cm
    assert model.estimate("small", "build_summary@1").calls == 6   # 5 ok + 1 failed
    assert model.estimate("small", "build_summary@1").ok_rate < 1.0


def test_cheapest_penalises_the_tail_not_the_typical_case(cm) -> None:
    """`small` is usually 10x faster, but its 900 ms outlier loses on p90.

    This is the intended behaviour, and it is the reason for using p90 at
    all. A budget is broken by the tail, not by the median. If you want the
    typical case instead, order on `p50_ms` and accept the overruns.
    """
    model, _ = cm
    assert model.estimate("small", "build_summary@1").p50_ms < \
           model.estimate("large", "build_summary@1").p50_ms
    assert model.cheapest(["small", "large"], "build_summary@1") == "large"


def test_cheapest_ignores_unmeasured_providers(cm) -> None:
    """`rare` looks fastest, but two samples do not earn a decision."""
    model, _ = cm
    assert model.cheapest(["rare", "large"], "build_summary@1") == "large"
    assert model.cheapest(["rare"], "build_summary@1") is None


def test_p90_on_a_small_sample_is_just_the_maximum(cm) -> None:
    """A known artefact, and the reason `confidence` exists.

    With six observations, the 90th percentile is the largest one. Treat a
    `low` confidence p90 as an upper bound, not as a typical cost. It becomes
    meaningful at `GOOD_SAMPLES`.
    """
    model, _ = cm
    est = model.estimate("small", "build_summary@1")
    assert est.confidence == "low"
    assert est.p90_ms == est.max_observed(model), "p90 == max at this sample size"


def test_pooled_estimate_without_a_schema(cm) -> None:
    model, _ = cm
    assert model.estimate("large").calls == 6


def test_calibration_report_is_slowest_first(cm) -> None:
    model, _ = cm
    rows = model.calibration_report()
    assert rows[0]["provider_id"] in {"small", "large"}
    assert rows[0]["p90_ms"] >= rows[-1]["p90_ms"]


def test_stale_manifests_flags_the_long_tail(cm) -> None:
    model, _ = cm
    flagged = {r["provider_id"] for r in model.stale_manifests()}
    assert "small" in flagged, "p90 far above p50 means the manifest is a guess"
    assert "large" not in flagged, "a steady provider is not flagged"


def test_rebuild_keeps_the_cost_view(cm, tmp_path: Path) -> None:
    model, ix = cm
    before = [tuple(r) for r in model.calibration_report()]
    ix.rebuild(tmp_path / "sessions")
    assert [tuple(r) for r in model.calibration_report()] == before
