"""Tests for event 0, section 4.6.

The hash exists to catch a specific silent failure: two runs that used the same
weights, took different routes, and looked identical afterwards. So most of
these tests are about what the hash *refuses*, not what it accepts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from picoharness.composition import (
    Composition,
    CompositionMismatch,
    MachineProfile,
    boot,
    check_replay,
)
from picoharness.ledger import Ledger

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def a_composition(**over) -> Composition:
    base = {
        "manifests": {"code-dfparse": {"implements": ["extract@1"], "kind": "code"}},
        "schemas": {"disk_usage@1": {"type": "object"}},
        "tools": {"get_disk_usage": {"version": 2}},
        "policy": {"quality_floor": 0.8},
    }
    base.update(over)
    return Composition(**base)


def a_machine(label="test-box", **over) -> MachineProfile:
    base = {
        "label": label,
        "system": "Linux",
        "machine": "aarch64",
        "cores": 4,
        "ram_mb": 8192,
        "costs": {"code-dfparse": {"p90_ms": 3.0, "residency": "pinned"}},
    }
    base.update(over)
    return MachineProfile(**base)


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------


def test_key_order_does_not_change_the_hash() -> None:
    """A hash that moves when a dict is rebuilt is worse than no hash."""
    one = Composition(policy={"a": 1, "b": 2}, schemas={"x@1": {"p": True}})
    two = Composition(schemas={"x@1": {"p": True}}, policy={"b": 2, "a": 1})
    assert one.digest() == two.digest()


def test_a_policy_change_changes_the_composition() -> None:
    """This is the case section 4.6 exists for: same models, different route."""
    assert a_composition().digest() != a_composition(policy={"quality_floor": 0.9}).digest()


def test_the_two_hashes_are_independent() -> None:
    """Same configuration, different box. The composition must not move."""
    comp = a_composition()
    slow = a_machine("orange-pi", machine="armv7l", cores=4, ram_mb=512)
    fast = a_machine("wsl2-x86", machine="x86_64", cores=16, ram_mb=32768)
    assert slow.digest() != fast.digest()
    assert comp.digest() == comp.digest()


def test_measured_cost_is_part_of_the_machine() -> None:
    """The policy filters on cost, so cost decides the route and must be hashed."""
    cheap = a_machine(costs={"code-dfparse": {"p90_ms": 3.0}})
    dear = a_machine(costs={"code-dfparse": {"p90_ms": 900.0}})
    assert cheap.digest() != dear.digest()


def test_detect_reads_the_real_box() -> None:
    profile = MachineProfile.detect("here")
    assert profile.system and profile.machine
    assert profile.cores is None or profile.cores >= 1
    assert profile.representative is True


def test_a_development_box_can_say_so() -> None:
    """A number measured under WSL2 is not an edge number. Record that."""
    dev = MachineProfile.detect("wsl2-x86", representative=False)
    assert dev.resolve()["representative"] is False
    assert dev.digest() != MachineProfile.detect("wsl2-x86").digest()


# --------------------------------------------------------------------------
# boot
# --------------------------------------------------------------------------


def test_boot_writes_event_zero(tmp_path) -> None:
    with Ledger(tmp_path / "s") as led:
        comp_hash, machine_hash = boot(led, a_composition(), a_machine())
        led.append("user_input", text="now the session may start")
        events = led.events()

    assert events[0]["seq"] == 0
    assert events[0]["type"] == "composition"
    assert events[0]["hash"] == comp_hash
    assert events[0]["machine"] == machine_hash


def test_boot_can_write_the_resolved_document(tmp_path) -> None:
    """If you cannot read it, you cannot audit it."""
    with Ledger(tmp_path / "s") as led:
        boot(led, a_composition(), a_machine(), snapshot_dir=tmp_path / "snapshots")
        event = led.events()[0]

    written = json.loads(Path(event["config"]).read_text(encoding="utf-8"))
    assert written["policy"] == {"quality_floor": 0.8}
    assert "python" in written["runtime"]


def test_the_resolved_document_names_the_runtime() -> None:
    """A build flag change is a composition change. Pin what you can see."""
    resolved = a_composition().resolve()
    assert resolved["runtime"]["python"]
    assert resolved["runtime"]["picoharness"]


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def test_replay_accepts_an_unchanged_composition() -> None:
    original = {"type": "composition", "hash": a_composition().digest(),
                "machine": a_machine().digest()}
    check_replay(original, a_composition(), a_machine())


def test_replay_refuses_a_changed_composition() -> None:
    original = {"type": "composition", "hash": a_composition().digest(),
                "machine": a_machine().digest()}
    with pytest.raises(CompositionMismatch, match="composition has changed"):
        check_replay(original, a_composition(policy={"quality_floor": 0.5}), a_machine())


def test_replay_on_other_hardware_is_a_warning_you_can_accept() -> None:
    """The plan is the same; the route may not be."""
    original = {"type": "composition", "hash": a_composition().digest(),
                "machine": a_machine("orange-pi", ram_mb=512).digest()}
    with pytest.raises(CompositionMismatch, match="different hardware"):
        check_replay(original, a_composition(), a_machine("wsl2-x86"))

    check_replay(original, a_composition(), a_machine("wsl2-x86"), allow_machine_change=True)


def test_replay_refuses_a_ledger_that_never_recorded_its_composition() -> None:
    with pytest.raises(CompositionMismatch, match="not `composition`"):
        check_replay({"type": "user_input"}, a_composition(), a_machine())
