"""Tests for the failure memory.

Run: python3 -m pytest test_failure_memory.py -q
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from picoharness.memory.episodic import EpisodicIndex
from picoharness.memory.failure import (
    REPORT_SQL,
    Avoidance,
    FailureMemory,
    classify_event,
    normalise_detail,
    signature_for,
)
from picoharness.memory.samples import BUILD_OK as S1
from picoharness.memory.samples import BUILD_OK_2 as S2
from picoharness.memory.samples import DEAD_END as S3
from picoharness.memory.samples import TAINTED as S4

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

# Session 1: the 350M extractor fails the schema, the 1.2B succeeds.

# Session 2: the same failure shape, different value in the message.

# Session 3: a gap and a dead end. Nothing recovers.

# Session 4: an error string that echoes untrusted content.


def _write(root: Path, sid: str, events: list[dict]) -> None:
    path = root / sid / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


@pytest.fixture()
def sessions(tmp_path: Path) -> Path:
    root = tmp_path / "sessions"
    for sid, ev in (("j1", S1), ("j2", S2), ("j3", S3), ("j4", S4)):
        _write(root, sid, ev)
    return root


@pytest.fixture()
def fm(tmp_path: Path, sessions: Path):
    ix = EpisodicIndex(tmp_path / "memory" / "episodic.db")
    ix.ingest_dir(sessions)
    yield FailureMemory(ix.conn), ix
    ix.close()


# --------------------------------------------------------------------------
# classification and signatures
# --------------------------------------------------------------------------


def test_normalise_detail_strips_the_variable_parts() -> None:
    a = normalise_detail("column 'user_id' not found in row 41")
    b = normalise_detail("column 'host' not found in row 9182")
    assert a == b == "column <q> not found in row #"


def test_paths_and_hashes_are_normalised() -> None:
    out = normalise_detail("poppler exited 1 on /var/data/inv-8891.pdf")
    assert "<path>" in out and "8891" not in out


def test_same_shape_gives_one_signature() -> None:
    kw = dict(capability="extract@1", tool="read_buildlog",
              provider_id="extract-350m-q4", schema_id="build_summary@1")
    s1 = signature_for("schema", detail="column 'a' not found in output", **kw)
    s2 = signature_for("schema", detail="column 'b' not found in output", **kw)
    s3 = signature_for("schema", detail="timeout after 30s", **kw)
    assert s1 == s2
    assert s1 != s3


def test_signature_never_contains_raw_detail() -> None:
    sig = signature_for("schema", detail="secret-token-abc")
    assert "secret" not in sig and len(sig) == 16


def test_classify_ignores_non_failure_events() -> None:
    assert classify_event({"seq": 1, "type": "fact_added"}) is None
    assert classify_event({"seq": 1, "type": "user_input"}) is None


def test_classify_sets_trust_by_kind() -> None:
    tainted = classify_event(
        {"seq": 1, "type": "validation_failed", "error": "bad"})
    clean = classify_event(
        {"seq": 2, "type": "capability_gap", "capability": "read_image@1"})
    assert tainted["detail_trust"] == "T1"
    assert clean["detail_trust"] == "T2"


# --------------------------------------------------------------------------
# ingest and resolution
# --------------------------------------------------------------------------


def test_failures_are_indexed(fm) -> None:
    _, ix = fm
    stats = ix.stats()
    assert stats["failures"] == 5     # 3 schema, 1 gap, 1 tool_failed
    assert stats["unresolved"] == 3   # the gap, the tool error, and j4


def test_escalation_is_recorded_as_the_remedy(fm) -> None:
    mem, _ = fm
    row = mem.conn.execute(
        "SELECT * FROM failure WHERE session_id = 'j1'").fetchone()
    assert row["resolution"] == "escalated"
    assert row["resolved_by"] == "extract-1.2b-q4"
    assert row["resolved_seq"] == 4


def test_declined_session_marks_its_failures(fm) -> None:
    mem, _ = fm
    kinds = {
        r["kind"]: r["resolution"]
        for r in mem.conn.execute("SELECT * FROM failure WHERE session_id = 'j3'")
    }
    assert kinds == {"capability_gap": "declined", "tool_error": "declined"}


def test_rebuild_keeps_failures(fm, sessions: Path) -> None:
    mem, ix = fm
    before = [tuple(r) for r in mem.conn.execute(
        "SELECT session_id, seq, kind, resolution FROM failure ORDER BY session_id, seq")]
    ix.rebuild(sessions)
    after = [tuple(r) for r in mem.conn.execute(
        "SELECT session_id, seq, kind, resolution FROM failure ORDER BY session_id, seq")]
    assert after == before


def test_reingest_does_not_duplicate(fm, sessions: Path) -> None:
    mem, ix = fm
    ix.ingest_dir(sessions)
    ix.ingest_dir(sessions)
    assert mem.conn.execute("SELECT COUNT(*) FROM failure").fetchone()[0] == 5


# --------------------------------------------------------------------------
# the planner path
# --------------------------------------------------------------------------


def test_avoidance_groups_and_reports_the_remedy(fm) -> None:
    mem, _ = fm
    hits = mem.avoidance(provider_id="extract-350m-q4")
    top = hits[0]
    assert isinstance(top, Avoidance)
    assert top.seen == 2, "the two build-log failures share one signature"
    assert top.remedy == "escalated"
    assert top.remedy_target == "extract-1.2b-q4"
    assert top.confidence == 1.0


def test_avoidance_never_returns_free_text(fm) -> None:
    """A detail string can carry content from a tool. The planner must not see it."""
    mem, _ = fm
    for hit in mem.avoidance(capability="extract@1", limit=20):
        blob = json.dumps(hit.__getstate__() if hasattr(hit, "__getstate__") else
                          {f: getattr(hit, f) for f in Avoidance.__slots__})
        assert "delete_backups" not in blob
        assert "Ignore previous instructions" not in blob
        assert "poppler" not in blob


def test_prompt_block_is_short_and_dense(fm) -> None:
    mem, _ = fm
    block = mem.prompt_block(mem.avoidance(capability="extract@1", limit=20))
    assert block.count("\n") <= 4
    assert len(block) < 400
    assert "escalated" in block


def test_prompt_block_is_empty_when_nothing_is_known(fm) -> None:
    mem, _ = fm
    assert mem.prompt_block(mem.avoidance(tool="never_used")) == ""


def test_unresolved_failure_says_so(fm) -> None:
    mem, _ = fm
    gap = mem.avoidance(capability="read_image@1")[0]
    assert gap.remedy is None
    assert "no known remedy" in gap.line()


# --------------------------------------------------------------------------
# the operator path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(REPORT_SQL))
def test_every_named_report_runs(fm, name: str) -> None:
    mem, _ = fm
    rows = mem.report(name, min_n=1, floor=0.9)
    assert isinstance(rows, list)


def test_provider_health_computes_a_pass_rate(fm) -> None:
    mem, _ = fm
    rows = {(r["provider_id"], r["schema_id"]): r
            for r in mem.report("provider_health", min_n=1)}
    good = rows[("extract-1.2b-q4", "build_summary@1")]
    assert good["facts_ok"] == 2 and good["failures"] == 0
    assert good["pass_rate"] == 1.0


def test_demotion_candidates_finds_the_weak_provider(fm) -> None:
    mem, _ = fm
    names = {r["provider_id"] for r in mem.report("demotion_candidates", floor=0.9, min_n=1)}
    assert "extract-350m-q4" in names
    assert "extract-1.2b-q4" not in names


def test_capability_gap_report_is_the_shopping_list(fm) -> None:
    mem, _ = fm
    rows = mem.report("gaps")
    assert [r["capability"] for r in rows] == ["read_image@1"]
    assert rows[0]["sessions"] == 1


def test_unresolved_report_excludes_what_recovered(fm) -> None:
    mem, _ = fm
    kinds = {r["kind"] for r in mem.report("unresolved")}
    assert "capability_gap" in kinds
    assert "tool_error" in kinds


def test_reports_accept_a_time_window(fm) -> None:
    mem, _ = fm
    assert mem.report("kinds", since="2026-08-01") != mem.report("kinds", since="0000")


def test_sql_is_readable_without_the_python(fm) -> None:
    """The operator must be able to run this from sqlite3 or R."""
    _mem, ix = fm
    raw = sqlite3.connect(ix.db_path)
    rows = raw.execute("SELECT provider_id, pass_rate FROM v_provider_health "
                       "ORDER BY pass_rate").fetchall()
    assert rows and rows[0][1] < 1.0
    raw.close()
