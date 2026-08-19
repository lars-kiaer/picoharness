"""Tests for the episodic index.

Run: python3 -m pytest test_episodic_index.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from picoharness.memory.samples import DISK_A as SESSION_A, DISK_B as SESSION_B
from picoharness.memory.episodic import (
    EpisodicIndex,
    RecallPolicy,
    TrustViolation,
    searchable_text,
)


# --------------------------------------------------------------------------
# fixtures: two synthetic session ledgers
# --------------------------------------------------------------------------


def _write(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path




@pytest.fixture()
def sessions(tmp_path: Path) -> Path:
    root = tmp_path / "sessions"
    _write(root / "job-0001" / "events.jsonl", SESSION_A)
    _write(root / "job-0002" / "events.jsonl", SESSION_B)
    return root


@pytest.fixture()
def index(tmp_path: Path, sessions: Path) -> EpisodicIndex:
    ix = EpisodicIndex(tmp_path / "memory" / "episodic.db")
    ix.ingest_dir(sessions)
    yield ix
    ix.close()


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------


def test_ingest_counts(index: EpisodicIndex) -> None:
    stats = index.stats()
    assert stats["episodes"] == 2
    assert stats["facts"] == 3
    assert stats["untrusted_facts"] == 1


def test_episode_metadata(index: EpisodicIndex) -> None:
    row = index.conn.execute(
        "SELECT * FROM episode WHERE session_id = 'job-0002'"
    ).fetchone()
    assert row["outcome"] == "partial"
    assert row["fail_count"] == 1
    assert row["gap_count"] == 1
    assert row["composition_hash"] == "sha256:41d0"


def test_ingest_is_idempotent(index: EpisodicIndex, sessions: Path) -> None:
    before = index.stats()
    index.ingest_dir(sessions)
    index.ingest_dir(sessions)
    assert index.stats() == before


def test_rebuild_equals_incremental(index: EpisodicIndex, sessions: Path) -> None:
    """The index is derived. Deleting it must lose nothing."""
    before = _snapshot(index)
    index.rebuild(sessions)
    assert _snapshot(index) == before


def test_growing_ledger_is_reindexed(index: EpisodicIndex, sessions: Path) -> None:
    ledger = sessions / "job-0002" / "events.jsonl"
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "seq": 7, "t": "2026-08-18T21:01:00Z", "type": "fact_added", "step": "s1",
            "provider": "code-dfparse", "schema": "disk_usage@1",
            "fact": {"mount": "/dev/sdb1", "disk_free_pct": 88, "state": "ok"},
        }) + "\n")
    index.ingest_dir(sessions)
    assert index.stats()["facts"] == 4


def _snapshot(ix: EpisodicIndex) -> list[tuple]:
    return [
        tuple(r)
        for r in ix.conn.execute(
            "SELECT session_id, seq, schema_id, trust, payload FROM fact "
            "ORDER BY session_id, seq"
        )
    ]


# --------------------------------------------------------------------------
# retrieval, path 1
# --------------------------------------------------------------------------


def test_recall_ranks_by_relevance(index: EpisodicIndex) -> None:
    hits = index.recall("disk timeout")
    assert hits, "expected at least one hit"
    assert hits[0].schema_id == "log_summary@2"
    assert hits[0].provenance.cite().endswith("#5")


def test_structured_lookup_without_query(index: EpisodicIndex) -> None:
    """The common case: a filter and no text. Newest first."""
    hits = index.recall(schema_id="disk_usage@1")
    assert [h.payload["disk_free_pct"] for h in hits] == [41, 12]


def test_field_range_needs_no_model(index: EpisodicIndex) -> None:
    hits = index.field_range("disk_free_pct", below=15)
    assert len(hits) == 1
    assert hits[0].payload["mount"] == "/dev/sda1"
    assert hits[0].provenance.session_id == "job-0001"


def test_interactive_budget_caps_the_limit(index: EpisodicIndex) -> None:
    policy = RecallPolicy(budget_class="interactive", limit=100)
    assert policy.effective_limit() == 5
    assert not policy.vector_allowed()
    assert RecallPolicy(budget_class="background").vector_allowed()


def test_similar_episodes(index: EpisodicIndex) -> None:
    rows = index.similar_episodes("disk pressure on the server")
    assert rows
    assert rows[0]["session_id"] in {"job-0001", "job-0002"}


def test_expired_facts_are_hidden(index: EpisodicIndex) -> None:
    index.conn.execute(
        "UPDATE fact SET valid_until = '2020-01-01T00:00:00Z' WHERE schema_id = 'log_summary@2'"
    )
    assert index.recall("timeout") == []
    assert index.recall("timeout", policy=RecallPolicy(include_expired=True))


def test_query_with_fts_operators_does_not_crash(index: EpisodicIndex) -> None:
    for nasty in ['disk AND OR NOT', 'disk"', "disk*", "(disk)", "", "   ", "NEAR/2"]:
        index.recall(nasty)


# --------------------------------------------------------------------------
# trust
# --------------------------------------------------------------------------


def test_trust_is_inherited_from_the_step(index: EpisodicIndex) -> None:
    log_fact = index.recall(schema_id="log_summary@2")[0]
    disk_fact = index.recall(schema_id="disk_usage@1")[0]
    assert log_fact.trust == "T1", "a fact from a log inherits the log's trust"
    assert disk_fact.trust == "T2"


def test_control_recall_excludes_untrusted_memory(index: EpisodicIndex) -> None:
    """A poisoned log line must not reach the planner three weeks later."""
    everything = index.recall("timeout error disk")
    assert any(h.trust == "T1" for h in everything)

    for_control = index.recall_for_control("timeout error disk")
    assert all(h.is_control_safe() for h in for_control)
    assert all(h.trust != "T1" for h in for_control)


def test_control_recall_raises_if_a_t1_fact_slips_through(index: EpisodicIndex) -> None:
    policy = RecallPolicy(allow_trust=frozenset({"T1"}), for_control=False)
    with pytest.raises(TrustViolation):
        # Force the guard: ask for control but allow only untrusted facts.
        index.recall = _leaky_recall(index)  # type: ignore[method-assign]
        index.recall_for_control("timeout", policy=policy)


def _leaky_recall(ix: EpisodicIndex):
    original = EpisodicIndex.recall

    def leaky(query="", **kwargs):
        kwargs.pop("policy", None)
        return original(ix, query, policy=RecallPolicy(allow_trust=frozenset({"T1"})))

    return leaky


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def test_searchable_text_keeps_key_names(index: EpisodicIndex) -> None:
    text = searchable_text({"disk_free_pct": 12, "nested": {"first_error": "I/O timeout"}})
    assert "disk free pct" in text
    assert "12" in text
    assert "nested first error" in text
    assert "I/O timeout" in text
