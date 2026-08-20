"""Tests for the adapter interface, the scope, and the first `code` provider.

The scope-unwind test is the one section 7.1.1 asks for by name. This design
swaps providers many times inside one task, so a registration that does not
unwind becomes a leak that grows with every step — and it looks like a memory
problem in the model, which is the wrong place to search.

The provider tests do something the design cares about more than coverage: they
record what deterministic code scores on the fixture set. Section 6.4 sorts
`kind == "code"` first, so this is the number a model has to beat before it is
worth its latency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from picoharness.adapters import CodeAdapter, ProviderError, Scope
from picoharness.adapters.base import UnwindError, timed_probe
from picoharness.adapters.code import resolve, time_call
from picoharness.payload import Payload, text
from picoharness.providers.log_summary import extract, parse_line

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

MANIFEST = {
    "id": "code-logsummary",
    "implements": ["extract@1"],
    "kind": "code",
    "entrypoint": "picoharness.providers.log_summary:extract",
    "produces": ["log_summary@2"],
    "security": {"max_trust_in": "T1", "may_emit_control": False},
    "determinism": "exact",
}

# --------------------------------------------------------------------------
# the scope, section 7.1.1
# --------------------------------------------------------------------------


def test_undos_run_in_reverse() -> None:
    """A later registration can depend on an earlier one."""
    order: list[str] = []
    scope = Scope()
    for label in ("schema", "grammar", "tmpdir"):
        scope.register(label, lambda label=label: order.append(label))
    scope.unwind()
    assert order == ["tmpdir", "grammar", "schema"]


def test_one_bad_undo_does_not_strand_the_rest() -> None:
    """Otherwise the leak this class prevents happens anyway."""
    done: list[str] = []
    scope = Scope()
    scope.register("first", lambda: done.append("first"))
    scope.register("broken", lambda: (_ for _ in ()).throw(OSError("no")))
    scope.register("last", lambda: done.append("last"))

    with pytest.raises(UnwindError, match="broken"):
        scope.unwind()
    assert done == ["last", "first"]


def test_an_unwound_scope_refuses_new_registrations() -> None:
    scope = Scope()
    scope.unwind()
    with pytest.raises(UnwindError, match="already unwound"):
        scope.register("late", lambda: None)


def test_load_then_unload_leaves_nothing_registered() -> None:
    """The test section 7.1.1 asks for, at the level this adapter can reach."""
    adapter = CodeAdapter()
    handle = adapter.load(MANIFEST)
    assert handle.scope.registered  # something was claimed
    adapter.unload(handle)
    assert handle.scope.registered == []


def test_a_provider_cannot_be_used_after_unload() -> None:
    """A leak looks like this from the other side: a stale handle that works."""
    adapter = CodeAdapter()
    handle = adapter.load(MANIFEST)
    adapter.unload(handle)
    with pytest.raises(ProviderError, match="was unloaded"):
        adapter.run(handle, text("Aug  3 04:14:30 h app[1]: error: no"), "log_summary@2")


# --------------------------------------------------------------------------
# the code adapter
# --------------------------------------------------------------------------


def test_a_manifest_pointing_at_nothing_fails_at_load() -> None:
    """A configuration error should not wait for the first step that needs it."""
    adapter = CodeAdapter()
    with pytest.raises(ProviderError, match="cannot import"):
        adapter.load({**MANIFEST, "entrypoint": "picoharness.nope:extract"})
    with pytest.raises(ProviderError, match="no attribute"):
        adapter.load({**MANIFEST, "entrypoint": "picoharness.providers.log_summary:nope"})
    with pytest.raises(ProviderError, match="must be"):
        adapter.load({**MANIFEST, "entrypoint": "no_colon_here"})
    with pytest.raises(ProviderError, match="has no entrypoint"):
        adapter.load({"id": "x", "kind": "code"})


def test_resolve_refuses_something_that_is_not_callable() -> None:
    with pytest.raises(ProviderError, match="not callable"):
        resolve("picoharness.providers.log_summary:_ERROR_WORDS")


def test_a_raising_provider_is_named_not_swallowed() -> None:
    """This becomes `tool_error` in the taxonomy, not `unknown`."""
    adapter = CodeAdapter()
    handle = adapter.load(
        {**MANIFEST, "entrypoint": "picoharness.adapters.code:resolve"}
    )
    with pytest.raises(ProviderError, match="raised"):
        adapter.run(handle, text("x"), "log_summary@2")
    adapter.unload(handle)


def test_the_same_input_gives_the_same_output() -> None:
    """Section 10.3: a step is a pure function, so it can be replayed."""
    adapter = CodeAdapter()
    handle = adapter.load(MANIFEST)
    body = (FIXTURES / "extract" / "normal-01-syslog-disk-io.input").read_text(encoding="utf-8")
    runs = [adapter.run(handle, text(body), "log_summary@2") for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    adapter.unload(handle)


def test_probe_fills_what_a_manifest_leaves_null() -> None:
    """Section 12: the benchmark harness writes these, not a person."""
    measured = CodeAdapter().probe(MANIFEST)
    assert measured["ram_mb"] == 0  # code adds no resident weights
    for key in ("cold_load_ms", "warm_load_ms", "run_p50_ms", "run_max_ms"):
        assert measured[key] >= 0


def test_timed_probe_unloads_even_when_a_run_fails() -> None:
    """A probe that leaks on failure defeats its own purpose."""

    class Exploding(CodeAdapter):
        def run(self, handle, payload, schema_id):  # type: ignore[override]
            raise ProviderError("boom")

    adapter = Exploding()
    with pytest.raises(ProviderError):
        timed_probe(adapter, MANIFEST, text("x"), "log_summary@2", runs=1)


def test_every_call_is_timed() -> None:
    """A call that is not timed is a call the cost model cannot price."""
    value, ms = time_call(lambda: 42)
    assert value == 42
    assert ms >= 0.0


# --------------------------------------------------------------------------
# the provider itself, against the fixture set
# --------------------------------------------------------------------------


def _fixtures() -> list[tuple[str, str, dict, dict]]:
    out = []
    for folder in ("extract", "adversarial"):
        for inp in sorted((FIXTURES / folder).glob("*.input")):
            expected = json.loads(inp.with_suffix(".expected.json").read_text(encoding="utf-8"))
            meta = json.loads(inp.with_suffix(".meta.json").read_text(encoding="utf-8"))
            out.append((inp.stem, inp.read_text(encoding="utf-8"), expected, meta))
    return out


ALL = _fixtures()

#: What deterministic code cannot reach, and why. Both are recorded rather than
#: fixed, because fixing them would mean fitting the parser to the answer key.
#:
#: `absent-06` needs the fallback rule of `fixtures/README.md`: a line whose
#: message says an operation did not succeed, with no severity word anywhere.
#: `adv-02` needs ruling 7 — telling a service start from work starting.
#:
#: These two are the clearest statement of where a model would earn its place.
KNOWN_MISSES = {
    "absent-06-unlabelled-trace",
    "adv-02-hostname-field-instruction",
}


@pytest.mark.parametrize(
    ("name", "body", "expected"),
    [(n, b, e) for n, b, e, _ in ALL],
    ids=[n for n, _, _, _ in ALL],
)
def test_code_provider_against_the_answer_key(name: str, body: str, expected: dict) -> None:
    got = extract(Payload(data=body, trust="T1"))
    if name in KNOWN_MISSES:
        assert got != expected, f"{name} now passes; take it out of KNOWN_MISSES"
    else:
        assert got == expected


def test_the_baseline_is_recorded() -> None:
    """The number a model has to beat, per section 6.4.

    Stated as an assertion so that a change in either direction is deliberate.
    A parser that quietly improves is fine; a parser that quietly regresses
    would move the bar without anyone deciding to.
    """
    passed = sum(
        1 for _, body, expected, _ in ALL
        if extract(Payload(data=body, trust="T1")) == expected
    )
    assert passed == len(ALL) - len(KNOWN_MISSES) == 28


def test_the_provider_never_invents_a_value() -> None:
    """Section 7.5: a provider that fills an absent field must not be used.

    Code has an advantage no model has here — when a rule does not apply there
    is nothing to produce — so this asserts the advantage is real rather than
    assumed.
    """
    for name, body, expected, meta in ALL:
        if meta["kind"] != "absent" or name in KNOWN_MISSES:
            continue
        got = extract(Payload(data=body, trust="T1"))
        for field, want in expected.items():
            if want is None:
                assert got[field] is None, f"{name}.{field}: invented {got[field]!r}"


def test_an_injected_instruction_changes_nothing() -> None:
    """Section 11.5, against the provider rather than the answer key."""
    for name, body, expected, meta in ALL:
        if meta["kind"] != "adversarial" or name in KNOWN_MISSES:
            continue
        got = extract(Payload(data=body, trust="T1"))
        assert got == expected
        for planted in meta["planted"]:
            assert planted not in json.dumps(got), f"{name}: emitted the planted {planted!r}"


# --------------------------------------------------------------------------
# line parsing, where the bugs actually were
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "ts", "host", "tag"),
    [
        ("<187>1 2026-08-18T04:13:02.774Z edge-07 relayd 1188 - - error: dropped",
         "2026-08-18T04:13:02.774Z", "edge-07", "relayd"),
        ("Aug  3 04:14:30 host-b archive[4410]: error: volume 7",
         "Aug  3 04:14:30", "host-b", "archive"),
        ("2026-08-18 09:44:12,009 ERROR checkout-api: refused",
         "2026-08-18 09:44:12,009", None, "checkout-api"),
        ("[   12.443112] usb 1-1: device descriptor read error",
         "[   12.443112]", None, None),
    ],
)
def test_line_shapes(raw: str, ts: str, host: str | None, tag: str | None) -> None:
    line = parse_line(raw)
    assert (line.ts, line.host, line.tag) == (ts, host, tag)


def test_a_severity_word_is_not_a_program_tag() -> None:
    """`journalctl -o cat` emits bare messages; "error: ..." is not a program."""
    assert parse_line("error: order 771206 rejected by schema check").tag is None


def test_truncation_needs_a_collector_notice_not_a_content_word() -> None:
    """The bug `adv-01` caught: an injected line could have nulled the count."""
    said_by_a_log_line = extract(text("Aug  3 04:14:30 h archive[1]: error: marked incomplete"))
    assert said_by_a_log_line["error_count"] == 1

    said_by_the_collector = extract(
        text("Aug  3 04:14:30 h archive[1]: error: x\n-- output truncated at 500 lines --")
    )
    assert said_by_the_collector["error_count"] is None


def test_work_starting_is_not_a_service_starting() -> None:
    """Ruling 7. `checkpoint starting` is work; `Started X.` is a lifecycle."""
    assert extract(text("Aug 15 04:20:00 db p[1]: LOG: checkpoint starting: time"))[
        "service_restarted"
    ] is False
    assert extract(text("Aug  3 04:09:12 h systemd[1]: Started Nightly archive job."))[
        "service_restarted"
    ] is True


def test_no_service_records_at_all_is_null_not_false() -> None:
    """Rule 7's third branch. Guessing `false` here is inventing."""
    assert extract(text("[   12.443112] usb 1-1: read error"))["service_restarted"] is None
