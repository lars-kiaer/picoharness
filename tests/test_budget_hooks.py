"""Tests for the task budget and the waterfall hooks.

Both exist to keep the main loop short, so both are tested without a loop.
"""

from __future__ import annotations

import pytest

from picoharness.budget import (
    CLASS_LIMITS,
    Breakers,
    BreakerState,
    Budget,
    Limit,
)
from picoharness.hooks import (
    POINTS,
    HookError,
    Hooks,
    Rejected,
    size_limit,
    trust_filter,
)
from picoharness.payload import text

# --------------------------------------------------------------------------
# a clock a test can drive
# --------------------------------------------------------------------------


class FakeClock:
    """Seconds, advanced by hand. Wall clock in a test must not be real."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance_ms(self, ms: float) -> None:
        self.t += ms / 1000.0


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------


def test_the_class_sets_the_limit() -> None:
    """The trigger decides the class, not the work. Section 5.3."""
    assert Budget("interactive").limit == CLASS_LIMITS["interactive"]
    assert Budget("batch").limit.wall_ms is None


def test_wall_clock_comes_from_the_clock_not_from_charges() -> None:
    """Time passes while a tool runs, whether or not anyone recorded it."""
    clock = FakeClock()
    b = Budget("attended", clock=clock)
    clock.advance_ms(9_000)
    assert b.remaining().wall_ms == pytest.approx(36_000, abs=50)
    assert b.spent.wall_ms == 0.0  # nothing was charged, and time still went


def test_the_tightest_axis_decides() -> None:
    """A budget with calls left but no time left is spent."""
    clock = FakeClock()
    b = Budget("attended", clock=clock)
    b.charge(model_calls=2)
    assert b.fraction_spent() == pytest.approx(0.2)
    clock.advance_ms(40_500)
    assert b.fraction_spent() == pytest.approx(0.9, abs=0.01)


def test_wind_down_before_exceeded() -> None:
    """At 80 % the runtime stops planning and answers with what it holds.

    A partial answer with a named gap is better than a timeout.
    """
    clock = FakeClock()
    b = Budget("attended", clock=clock)
    clock.advance_ms(36_000)
    assert b.should_wind_down()
    assert not b.exceeded()
    clock.advance_ms(10_000)
    assert b.exceeded()


def test_an_unlimited_axis_does_not_count() -> None:
    b = Budget("batch", clock=FakeClock())
    b.charge(model_calls=1000, cpu_ms=10**9)
    assert b.fraction_spent() == 0.0
    assert not b.exceeded()


def test_the_cascade_opens_at_background() -> None:
    """Section 9.3: vector search costs an embedding, so interactive says no."""
    assert not Budget("interactive").allows_vector_search()
    assert not Budget("attended").allows_vector_search()
    assert Budget("background").allows_vector_search()


def test_json_shape_matches_the_design() -> None:
    clock = FakeClock()
    b = Budget("attended", clock=clock)
    clock.advance_ms(18_400)
    b.charge(model_calls=4, cpu_ms=51_200)
    doc = b.to_json()
    assert doc["class"] == "attended"
    assert doc["limit"]["wall_ms"] == 45_000
    assert doc["spent"]["model_calls"] == 4
    assert doc["spent"]["wall_ms"] == pytest.approx(18_400, abs=1)


def test_a_sub_task_gets_a_share_of_what_is_left() -> None:
    clock = FakeClock()
    b = Budget("attended", clock=clock)
    clock.advance_ms(5_000)
    half = b.tightened(0.5)
    assert half.wall_ms == pytest.approx(20_000, abs=50)


def test_custom_limits_override_the_class() -> None:
    b = Budget("interactive", limit=Limit(wall_ms=100, model_calls=1), clock=FakeClock())
    assert b.limit.wall_ms == 100


# --------------------------------------------------------------------------
# breakers
# --------------------------------------------------------------------------


def test_a_breaker_reports_rather_than_raises() -> None:
    """When a breaker opens the system must still answer. Section 5.3."""
    state, breakers = BreakerState(), Breakers(max_steps=2)
    for _ in range(3):
        state.note_step()
    assert state.tripped(breakers) == "max_steps"


def test_retries_are_counted_per_step() -> None:
    state, breakers = BreakerState(), Breakers(max_retries_per_step=2)
    for _ in range(3):
        state.note_retry("s1")
    state.note_retry("s2")
    assert state.tripped(breakers, "s1") == "max_retries_per_step"
    assert state.tripped(breakers, "s2") is None


def test_a_budget_alone_cannot_stop_a_fast_loop() -> None:
    """The reason breakers exist. Two steps alternating forever cost no time."""
    clock = FakeClock()
    b = Budget("batch", clock=clock)
    state = BreakerState()
    for _ in range(20):
        state.note_step()
    assert not b.exceeded()
    assert state.tripped(b.breakers) == "max_steps"


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------


def test_a_chain_passes_the_value_along() -> None:
    hooks = Hooks()
    hooks.on("on_reduce", lambda v, nxt: nxt(v + 1), name="add")
    hooks.on("on_reduce", lambda v, nxt: nxt(v * 10), name="times")
    assert hooks.run("on_reduce", 1) == 20


def test_no_listener_is_the_identity() -> None:
    assert Hooks().run("on_commit", "unchanged") == "unchanged"


def test_a_listener_that_does_not_call_next_stops_the_chain() -> None:
    """Short-circuiting is legitimate for a filter. Rejecting is different."""
    hooks = Hooks()
    hooks.on("on_output", lambda v, nxt: "short", name="stop")
    hooks.on("on_output", lambda v, nxt: nxt("never reached"), name="after")
    assert hooks.run("on_output", "in") == "short"


def test_calling_next_twice_is_an_error() -> None:
    """A chain that forks is not a waterfall, and the result would be arbitrary."""
    hooks = Hooks()
    hooks.on("on_reduce", lambda v, nxt: (nxt(v), nxt(v)), name="forks")
    with pytest.raises(HookError, match="more than once"):
        hooks.run("on_reduce", 1)


def test_an_unknown_point_is_refused() -> None:
    with pytest.raises(HookError, match="unknown hook point"):
        Hooks().on("on_whatever", lambda v, nxt: nxt(v))


def test_rejection_always_carries_an_event() -> None:
    """Rule 2, made structural: a silent rejection cannot be expressed."""
    with pytest.raises(Rejected) as caught:
        raise Rejected("validation_failed", error="no", detail_trust="T2")
    assert caught.value.event_type == "validation_failed"
    assert caught.value.fields["error"] == "no"


def test_hooks_are_part_of_the_composition() -> None:
    """A different set of hooks is a different system. Section 4.6."""
    hooks = Hooks()
    hooks.on("on_prepare", trust_filter("T1"))
    hooks.on("on_output", size_limit(8192))
    assert hooks.resolve() == {
        "on_prepare": ["trust_filter(T1)"],
        "on_output": ["size_limit(8192)"],
    }


def test_every_point_in_the_design_exists() -> None:
    assert POINTS == ("on_select", "on_prepare", "on_output", "on_reduce", "on_commit")


# --------------------------------------------------------------------------
# the two listeners the runtime installs
# --------------------------------------------------------------------------


def test_size_limit_truncates_and_leaves_a_trace() -> None:
    hooks = Hooks().on("on_output", size_limit(10))
    out = hooks.run("on_output", text("x" * 100))
    assert out.nbytes == 10
    assert out.meta["truncated_from"] == 100


def test_size_limit_leaves_a_small_payload_alone() -> None:
    hooks = Hooks().on("on_output", size_limit(10))
    assert hooks.run("on_output", text("short")).meta == {}


def test_trust_filter_refuses_data_above_the_ceiling() -> None:
    """Section 11.2 in one place: a T1 payload never reaches a T2-only provider."""
    hooks = Hooks().on("on_prepare", trust_filter("T2"))
    with pytest.raises(Rejected) as caught:
        hooks.run("on_prepare", text("a poisoned log line", trust="T1"))
    assert caught.value.event_type == "validation_failed"
    assert caught.value.fields["detail_trust"] == "T2"


def test_trust_filter_passes_what_it_should() -> None:
    hooks = Hooks().on("on_prepare", trust_filter("T1"))
    for level in ("T0", "T1", "T2"):
        assert hooks.run("on_prepare", text("fine", trust=level)).trust == level
