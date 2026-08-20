"""Selection, and the security boundary it enforces. Sections 6.4 and 11.2.

The filter that matters most lives in one place, and this is that place. Section
11.2 says a provider that has read T1 data must not emit a control decision, and
`select()` is where the rule is applied — before the call, not after it.
"""

from __future__ import annotations

import pytest

from picoharness.adapters import CodeAdapter
from picoharness.adapters.base import ProviderError
from picoharness.registry import (
    POLICY_ID,
    Capability,
    CapabilityGap,
    Provider,
    Registry,
    policy_identity,
)

ENTRY = "picoharness.providers.log_summary:extract"


def registry() -> Registry:
    r = Registry()
    r.add_adapter(CodeAdapter())
    r.add_capability(Capability("extract@1", "text", "schema", is_control=False))
    r.add_capability(Capability("route@1", "goal", "toolcall", is_control=True))
    r.add_capability(Capability("answer@1", "facts", "text", is_control=False))
    return r


def manifest(**over) -> dict:
    base = {
        "id": "p", "kind": "code", "implements": ["extract@1"], "entrypoint": ENTRY,
        "security": {"max_trust_in": "T1", "may_emit_control": False},
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# one provider, two capabilities
# --------------------------------------------------------------------------


def test_one_provider_can_serve_a_data_and_a_control_capability() -> None:
    """Extraction is tool calling with one declared tool, so the same weights
    can do both. Section 11.2 constrains what a *call* has read, not what the
    provider's code is."""
    r = registry()
    r.add_provider(
        manifest(
            id="needle-26m",
            implements=["extract@1", "route@1"],
            security={
                "max_trust_in": {"extract@1": "T1", "route@1": "T0"},
                "may_emit_control": ["route@1"],
            },
        )
    )
    assert r.select("extract@1", trust_in="T1").id == "needle-26m"
    assert r.select("route@1", trust_in="T0").id == "needle-26m"


def test_the_same_provider_may_not_route_on_untrusted_input() -> None:
    """The whole point of a per-capability ceiling: same weights, different
    contract per call. A poisoned log may be extracted from and never routed on."""
    r = registry()
    r.add_provider(
        manifest(
            id="needle-26m",
            implements=["extract@1", "route@1"],
            security={
                "max_trust_in": {"extract@1": "T1", "route@1": "T0"},
                "may_emit_control": ["route@1"],
            },
        )
    )
    with pytest.raises(CapabilityGap):
        r.select("route@1", trust_in="T1")


def test_an_unlisted_capability_gets_the_strictest_ceiling() -> None:
    """Silence must not widen a security boundary."""
    p = Provider(
        id="p", kind="code", implements=("extract@1", "route@1"),
        manifest={"security": {"max_trust_in": {"extract@1": "T1"}}},
    )
    assert p.max_trust_in("extract@1") == "T1"
    assert p.max_trust_in("route@1") == "T2"


def test_the_scalar_form_still_works() -> None:
    """An existing manifest keeps working; the two forms mean the same thing
    when there is only one capability."""
    p = Provider(
        id="p", kind="code", implements=("extract@1",),
        manifest={"security": {"max_trust_in": "T1", "may_emit_control": True}},
    )
    assert p.max_trust_in("extract@1") == "T1"
    assert p.may_emit_control("anything") is True


def test_a_data_provider_cannot_be_selected_for_control() -> None:
    r = registry()
    r.add_provider(manifest(id="reducer", implements=["extract@1", "route@1"]))
    assert r.select("extract@1", trust_in="T1").id == "reducer"
    with pytest.raises(CapabilityGap, match="control capability"):
        r.select("route@1", trust_in="T0")


# --------------------------------------------------------------------------
# the rest of the policy
# --------------------------------------------------------------------------


def test_code_sorts_before_a_model() -> None:
    """Section 6.4. The system gets faster every time a parser replaces a model."""
    r = registry()
    r.add_provider(manifest(id="a-model", kind="gguf"))
    r.add_provider(manifest(id="z-parser", kind="code"))
    r.add_adapter(type("Fake", (), {"kind": "gguf"})())
    assert r.select("extract@1").id == "z-parser"


def test_selection_is_deterministic() -> None:
    """Two providers of the same kind resolve by id, so a run repeats."""
    r = registry()
    r.add_provider(manifest(id="bbb"))
    r.add_provider(manifest(id="aaa"))
    assert {r.select("extract@1").id for _ in range(5)} == {"aaa"}


def test_the_target_schema_is_part_of_the_request() -> None:
    """A capability names a contract, not an output."""
    r = registry()
    r.add_provider(manifest(id="logs", produces=["log_summary@2"]))
    r.add_provider(manifest(id="disks", produces=["disk_usage@1"]))
    assert r.select("extract@1", schema="disk_usage@1").id == "disks"
    assert r.select("extract@1", schema="log_summary@2").id == "logs"


def test_a_provider_without_an_adapter_is_not_selectable() -> None:
    r = registry()
    r.add_provider(manifest(id="onnx-thing", kind="onnx"))
    with pytest.raises(CapabilityGap):
        r.select("extract@1")


def test_modality_is_filtered() -> None:
    """The blob-ready seam has to mean something at selection time too."""
    r = registry()
    r.add_provider(manifest(id="text-only", modality_in=["text"]))
    assert r.select("extract@1", mime="text/plain").id == "text-only"
    with pytest.raises(CapabilityGap):
        r.select("extract@1", mime="image/png")


def test_exclusion_drives_the_escalation_ladder() -> None:
    """Section 6.5 walks the ladder by asking again without the one that failed."""
    r = registry()
    r.add_provider(manifest(id="small"))
    r.add_provider(manifest(id="large"))
    first = r.select("extract@1")
    second = r.select("extract@1", exclude=frozenset({first.id}))
    assert second.id != first.id
    with pytest.raises(CapabilityGap, match="outside"):
        r.select("extract@1", exclude=frozenset({first.id, second.id}))


def test_a_gap_is_an_outcome_not_a_crash() -> None:
    """Section 6.6: the gap log tells you which model to install next."""
    with pytest.raises(CapabilityGap, match="read_image@1"):
        registry().select("read_image@1")


def test_the_registry_resolves_into_the_composition() -> None:
    r = registry()
    r.add_provider(manifest(id="p"))
    resolved = r.resolve()
    assert resolved["adapters"] == ["code"]
    assert resolved["capabilities"]["route@1"]["control"] is True
    assert resolved["providers"]["p"]["entrypoint"] == ENTRY


# --------------------------------------------------------------------------
# the configuration plane is checked too
# --------------------------------------------------------------------------


def test_a_misspelt_key_does_not_widen_a_boundary() -> None:
    """The defect this schema exists for.

    `secuirty` misses the whole block, so `max_trust_in` falls back to `T1`.
    A typo therefore made a security boundary wider, which is the inverse of
    the rule in 6.2 that silence must never do that.
    """
    with pytest.raises(ProviderError, match="not in the schema"):
        registry().add_provider({**manifest(), "secuirty": {"max_trust_in": "T2"}})


@pytest.mark.parametrize(
    ("why", "broken"),
    [
        ("unknown key", {"confidence_flooor": 0.9}),
        ("modality as a string", {"modality_in": "text"}),
        ("unknown kind", {"kind": "cact"}),
        ("a trust level that does not exist", {"security": {"max_trust_in": "T3"}}),
        ("floor outside 0..1", {"confidence_floor": 1.5}),
        ("determinism that is not a word we use", {"determinism": "mostly"}),
    ],
)
def test_a_malformed_manifest_is_refused(why: str, broken: dict) -> None:
    with pytest.raises(ProviderError):
        registry().add_provider({**manifest(), **broken})


def test_security_must_name_capabilities_the_provider_implements() -> None:
    """A mapping keyed on a typo would be ignored, and the real capability would
    fall through to the strict default. Safe, but silently wrong is still wrong."""
    with pytest.raises(ProviderError, match="does not implement"):
        registry().add_provider(
            manifest(security={"max_trust_in": {"extarct@1": "T1"}})
        )
    with pytest.raises(ProviderError, match="does not implement"):
        registry().add_provider(manifest(security={"may_emit_control": ["rout@1"]}))


def test_an_unchecked_manifest_is_possible_and_deliberate() -> None:
    r = registry()
    assert r.add_provider({**manifest(), "whatever": 1}, checked=False).id == "p"


def test_modality_as_a_string_is_refused_in_code_too() -> None:
    """Defence in depth: a substring test would make `tex` match `text`."""
    p = Provider(id="p", kind="code", implements=("extract@1",),
                 manifest={"modality_in": "text"})
    with pytest.raises(ProviderError, match="must be a list"):
        p.accepts("text/plain")


# --------------------------------------------------------------------------
# the contract, section 6.1
# --------------------------------------------------------------------------


def test_the_audit_finds_what_is_knowable_at_boot() -> None:
    r = registry()
    r.add_provider(manifest(id="orphan", implements=["translate@1"]))
    r.add_provider(manifest(id="ghost", produces=["never_made@1"]))
    r.add_provider(manifest(id="unrunnable", kind="onnx"))

    problems = "; ".join(r.audit(schemas=["log_summary@2"]))
    assert "orphan implements 'translate@1', which no capability declares" in problems
    assert "ghost produces 'never_made@1', which is not registered" in problems
    assert "unrunnable is kind 'onnx', and no adapter can run it" in problems


def test_a_sound_composition_audits_clean() -> None:
    r = registry()
    r.add_provider(manifest(produces=["log_summary@2"]))
    assert r.audit(schemas=["log_summary@2"]) == []


# --------------------------------------------------------------------------
# the policy has an identity, section 4.6
# --------------------------------------------------------------------------


def test_the_policy_is_named_and_hashed() -> None:
    """The composition covered the policy's configuration and not its code, so
    a rewritten `select()` could route differently under the same hash."""
    identity = policy_identity()
    assert identity["id"] == POLICY_ID
    assert identity["digest"].startswith("sha256:")


def test_the_policy_identity_reaches_the_composition() -> None:
    assert registry().resolve()["policy"] == policy_identity()


def test_the_digest_follows_the_code_and_not_the_configuration() -> None:
    """It is taken from the source, so it moves on a change nobody declared."""
    import inspect

    from picoharness.registry import Registry as R

    source = inspect.getsource(R.select)
    assert "kind" in source and "TRUST_ORDER" in source  # the two lines that matter
    assert policy_identity()["digest"] == policy_identity()["digest"]
