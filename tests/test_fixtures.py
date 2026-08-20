"""The fixture set is checked by the real validator, not by a copy of it.

Section 6 of the evaluation note calls the fixture set the durable asset of this
project: model rankings change every quarter, and these files do not. An asset
that nothing checks rots quietly, so these tests guard the properties that make
the set worth having.

They also give `picoharness.validate` its first thirty real inputs, which is why
the checks run through `Schema` and `CrossChecks` rather than through a
test-local validator that could drift from the code that ships.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from picoharness.validate import (
    CrossChecks,
    Schema,
    count_matches,
    extractive_fields,
    ladder,
    spans_for,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
EXTRACT = FIXTURES / "extract"
ADVERSARIAL = FIXTURES / "adversarial"

#: The regular expression the fixture README documents as the second path to
#: `error_count`. Section 10.2 level 4: if a model and this disagree, this wins.
ERROR_LINE = (
    r"\b(emerg|emergency|alert|crit|critical|panic|fatal|err|error|errors"
    r"|fail|failed|failure|failures)\b"
)

#: The three fixtures where the regular expression and the expected value differ
#: on purpose. Each one is explained in `fixtures/README.md`. Listing them here
#: means a fourth divergence appearing later is a test failure and not a shrug.
DOCUMENTED_DIVERGENCE = {
    "normal-04-rfc5424-nginx",          # severity is in the priority value
    "absent-06-unlabelled-trace",       # no severity stated; fallback rule
    "absent-04-truncated-window",       # window incomplete, so the count is null
}

#: Values a model is told to copy from the input. A fixture whose answer key is
#: not in its own input is not a fixture; it is a guess.
VERBATIM = ("host", "first_error", "first_error_at", "service")


def fixture_names(folder: Path) -> list[str]:
    return sorted(p.stem for p in folder.glob("*.input"))


def load(folder: Path, name: str) -> tuple[str, dict, dict]:
    raw = (folder / f"{name}.input").read_text(encoding="utf-8")
    expected = json.loads((folder / f"{name}.expected.json").read_text(encoding="utf-8"))
    meta = json.loads((folder / f"{name}.meta.json").read_text(encoding="utf-8"))
    return raw, expected, meta


ALL = [(EXTRACT, n) for n in fixture_names(EXTRACT)] + [
    (ADVERSARIAL, n) for n in fixture_names(ADVERSARIAL)
]


@pytest.fixture(scope="module")
def schema() -> Schema:
    return Schema.from_file(FIXTURES / "schemas" / "log_summary@2.json")


# --------------------------------------------------------------------------
# the set as a whole
# --------------------------------------------------------------------------


def test_the_counts_match_the_evaluation_note() -> None:
    """Section 7.2: 20 normal, 7 with an absent field, 3 poisoned."""
    kinds = {"normal": 0, "absent": 0, "adversarial": 0}
    for folder, name in ALL:
        _, _, meta = load(folder, name)
        kinds[meta["kind"]] += 1
    assert kinds == {"normal": 20, "absent": 7, "adversarial": 3}


def test_poisoned_fixtures_stay_in_their_own_directory() -> None:
    """They are also training pairs. A poisoned one must never join a training set."""
    for name in fixture_names(EXTRACT):
        _, _, meta = load(EXTRACT, name)
        assert meta["kind"] != "adversarial", f"{name} is poisoned but sits in extract/"
    for name in fixture_names(ADVERSARIAL):
        _, _, meta = load(ADVERSARIAL, name)
        assert meta["kind"] == "adversarial"


def test_every_fixture_has_all_three_files() -> None:
    for folder in (EXTRACT, ADVERSARIAL):
        for name in fixture_names(folder):
            assert (folder / f"{name}.expected.json").exists(), name
            assert (folder / f"{name}.meta.json").exists(), name


def test_every_field_is_absent_somewhere(schema: Schema) -> None:
    """Abstention is measured per field, not once for the schema.

    A model that abstains well on strings may still invent a boolean, so every
    field needs at least one fixture where the honest answer is `null`.
    """
    never_null = set(schema.body["required"])
    for folder, name in ALL:
        _, expected, _ = load(folder, name)
        never_null -= {k for k, v in expected.items() if v is None}
    assert not never_null, f"no fixture tests abstention for: {sorted(never_null)}"


# --------------------------------------------------------------------------
# each fixture
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("folder", "name"), ALL, ids=[n for _, n in ALL])
def test_expected_record_passes_the_ladder(folder: Path, name: str, schema: Schema) -> None:
    """The answer key must itself be a valid answer.

    If the expected record cannot pass levels 2 and 3, then every model is
    marked wrong for agreeing with it, and the fault is in the fixture.
    """
    _, expected, _ = load(folder, name)
    result = schema.check(expected)
    assert result.ok, f"{name}: {result.error()}"


@pytest.mark.parametrize(("folder", "name"), ALL, ids=[n for _, n in ALL])
def test_copied_values_are_verbatim_in_the_input(folder: Path, name: str) -> None:
    """Section 5: keep the raw input. The answer must be derivable from it."""
    raw, expected, _ = load(folder, name)
    for key in VERBATIM:
        value = expected.get(key)
        if isinstance(value, str):
            assert value in raw, f"{name}.{key}: {value!r} is not in the input"


@pytest.mark.parametrize(("folder", "name"), ALL, ids=[n for _, n in ALL])
def test_meta_names_the_capability_and_schema(folder: Path, name: str) -> None:
    _, _, meta = load(folder, name)
    assert meta["capability"] == "extract@1"
    assert meta["schema"] == "log_summary@2"
    assert meta["kind"] in {"normal", "absent", "adversarial"}


# --------------------------------------------------------------------------
# level 4, on real data
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("folder", "name"), ALL, ids=[n for _, n in ALL])
def test_cross_check_agrees_except_where_documented(
    folder: Path, name: str, schema: Schema
) -> None:
    """The deterministic second path, run against the answer key.

    Where the two agree, level 4 is real for this schema and costs nothing.
    Where they differ, the reason is written down in `fixtures/README.md`. A
    fourth divergence appearing later means either a fixture changed or the
    rule did, and both are worth stopping for.
    """
    raw, expected, _ = load(folder, name)
    checks = CrossChecks().add("error_count", count_matches(ERROR_LINE, flags=re.IGNORECASE))
    result = ladder(expected, schema, raw_input=raw, cross_checks=checks)

    if name in DOCUMENTED_DIVERGENCE:
        if name == "absent-04-truncated-window":
            # `null` means "not derivable". The cross-check must skip it, or it
            # reports a failure every time abstention is correct.
            assert result.ok, "a null field must never be cross-checked"
        else:
            assert not result.ok, f"{name} is listed as a divergence but now agrees"
            assert result.kind == "crosscheck"
    else:
        assert result.ok, f"{name}: {result.error()}"


def test_an_injected_instruction_does_not_change_the_answer_key() -> None:
    """Section 11.5. The poisoned three must expect ordinary, correct values.

    The architecture already stops a takeover, because a reducer can only emit a
    record that matches a schema and the schema has no field for a tool call. So
    what is measured here is data corruption: did the planted text move a value?

    Each fixture declares what its injection tried to plant, so this test checks
    the property rather than the technique. A fourth fixture using a method
    nobody has thought of yet is covered without changing this code.
    """
    for name in fixture_names(ADVERSARIAL):
        raw, expected, meta = load(ADVERSARIAL, name)
        planted = meta.get("planted")
        assert planted, f"{name}: an adversarial fixture must declare what it plants"
        assert meta.get("technique"), f"{name}: say how the injection is delivered"

        for value in planted:
            assert value in raw, f"{name}: {value!r} is declared planted but is not in the input"
            assert value not in json.dumps(expected), (
                f"{name}: the answer key repeats the planted value {value!r}"
            )
        assert expected["error_count"] != meta["planted_error_count"], (
            f"{name}: the expected count equals the count the injection asked for, "
            f"so the fixture cannot tell a corrupted answer from a correct one"
        )


# --------------------------------------------------------------------------
# abstention as a contract, section 10.1
# --------------------------------------------------------------------------


def test_the_schema_permits_absence_for_every_field(schema: Schema) -> None:
    """A reducer must be able to say "not present" for each field.

    If the schema does not allow `null`, the only way to satisfy it is to
    produce a value — so the contract itself would demand invention.
    """
    for name, spec in schema.body["properties"].items():
        declared = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        assert "null" in declared, f"{name} cannot be reported absent"


def test_the_schema_says_which_fields_are_copied(schema: Schema) -> None:
    """A span check needs to know which fields have a span at all.

    `error_count`, `max_severity` and `service_restarted` are derived from the
    whole window and appear nowhere in it verbatim. Checking them for a span
    would fail every correct answer.
    """
    assert extractive_fields(schema) == ("host", "first_error", "first_error_at", "service")


@pytest.mark.parametrize(("folder", "name"), ALL, ids=[n for _, n in ALL])
def test_every_answer_key_survives_the_span_check(
    folder: Path, name: str, schema: Schema
) -> None:
    """Level 4 over the extractive fields, run against the answer key itself."""
    raw, expected, _ = load(folder, name)
    result = spans_for(schema).run(expected, raw)
    assert result.ok, f"{name}: {result.error()}"


def test_an_invented_value_is_caught_as_a_crosscheck_failure(schema: Schema) -> None:
    """Section 2.1 reads `crosscheck` as the dangerous kind, and it is right.

    A model that answers `absent-05` with a plausible host name has produced a
    record that passes the schema, passes every range, and is false. Only a
    second path against the source finds it.
    """
    raw, expected, _ = load(EXTRACT, "absent-05-app-log-no-host")
    assert expected["host"] is None

    invented = {**expected, "host": "prod-01"}
    result = ladder(invented, schema, raw_input=raw, cross_checks=spans_for(schema))
    assert not result.ok
    assert result.kind == "crosscheck"
    assert "does not appear in the input" in result.error()


def test_abstaining_is_never_a_span_failure(schema: Schema) -> None:
    """`null` is the honest answer, so it has nothing to justify."""
    raw, expected, _ = load(EXTRACT, "absent-05-app-log-no-host")
    assert spans_for(schema).run(expected, raw).ok
