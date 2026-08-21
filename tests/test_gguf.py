"""v2's exit test, and the two pins that must hold before a model is loaded.

> You can swap code for model in the manifest, with no change to the runtime.

Section 15 calls that the real test of the design. What must not change is the
runtime: the loop, the ledger, the validation ladder, the budget. What does
change is the composition — a different manifest, and an adapter registered for
the kind it names. Installing an adapter is provisioning, in the same way that
putting the weights on the box is provisioning, and `Registry.resolve()` lists
the adapters for exactly that reason.

These tests need the weights and the binding, so they skip where either is
missing. That is every Windows machine and every CI runner today. The suite
must stay green there, or the skip is a way of not noticing a break.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("llama_cpp", reason="the gguf adapter needs llama-cpp-python")

from picoharness.adapters.base import ProviderError
from picoharness.adapters.gguf import GgufAdapter
from picoharness.budget import Budget
from picoharness.runtime import Step
from picoharness.validate import Schema
from tests.test_runtime import DISK_SCHEMA, MANIFESTS, build

REPO = Path(__file__).resolve().parent.parent
MODEL_ROOT = Path(os.environ.get("PICOHARNESS_MODELS", Path.home() / "models"))
MANIFEST = json.loads(
    (REPO / "manifests" / "extract-350m-q4.manifest.json").read_text(encoding="utf-8")
)
WEIGHTS = MODEL_ROOT / MANIFEST["file"]

pytestmark = pytest.mark.skipif(
    not WEIGHTS.exists(), reason=f"no weights at {WEIGHTS}; provisioning is a separate step"
)

SCHEMAS = {
    "log_summary@2": Schema.from_file(REPO / "fixtures" / "schemas" / "log_summary@2.json"),
    "disk_usage@1": DISK_SCHEMA,
}


def an_adapter() -> GgufAdapter:
    return GgufAdapter(
        model_root=MODEL_ROOT, prompt_root=REPO / "prompts", schemas=SCHEMAS, n_threads=4
    )


def open_files() -> int:
    """Descriptors this process holds. The scope unwind test reads it."""
    return len(list(Path("/proc/self/fd").iterdir()))


# --------------------------------------------------------------------------
# the pins, section 10.3
# --------------------------------------------------------------------------


def test_weights_that_do_not_match_the_manifest_are_refused() -> None:
    """A pin nobody checks is a comment. This is the check."""
    moved = {**MANIFEST, "sha256": "sha256:" + "0" * 64}
    with pytest.raises(ProviderError, match="not the file this manifest pins"):
        an_adapter().load(moved)


def test_a_manifest_that_pins_nothing_is_refused() -> None:
    unpinned = {k: v for k, v in MANIFEST.items() if k != "sha256"}
    with pytest.raises(ProviderError, match="pins no sha256"):
        an_adapter().load(unpinned)


def test_a_prompt_that_moved_under_the_manifest_is_refused() -> None:
    """The envelope is pinned by hash, so a rewritten file cannot pass as it."""
    moved = {**MANIFEST, "system_prompt_sha256": "sha256:" + "0" * 64}
    with pytest.raises(ProviderError, match="not the prompt this manifest pins"):
        an_adapter().load(moved)


# --------------------------------------------------------------------------
# reversible registration, section 7.1.1
# --------------------------------------------------------------------------


def test_load_and_unload_leave_nothing_behind() -> None:
    """A load that does not unwind is a leak that grows with every step.

    It would also look like a memory fault in the model, which is the wrong
    place to search, so this asserts the file descriptors as well as the scope.
    """
    adapter = an_adapter()
    before = open_files()
    handle = adapter.load(MANIFEST)
    assert handle.scope.registered, "load() registered nothing, so unload() undoes nothing"
    adapter.unload(handle)
    assert handle.scope.registered == []
    assert open_files() == before


# --------------------------------------------------------------------------
# the exit test
# --------------------------------------------------------------------------


ONE_LOG_STEP = [Step("s2", "read_log", {"path": "syslog"}, subject="host-a")]


def a_plan(data: Path) -> list[Step]:
    return [
        Step(s.id, s.tool, {"path": str(data / s.args["path"])}, s.subject) for s in ONE_LOG_STEP
    ]


def run_with(tmp_path: Path, manifests, session: str, adapters=()):
    runtime, data = build(
        tmp_path,
        manifests=manifests,
        session=session,
        adapters=adapters,
        budget=Budget("background"),
    )
    outcome = runtime.run("summarise the syslog on host-a", a_plan(data))
    runtime.ledger.close()
    return runtime, outcome


def test_the_swap_from_code_to_model_is_a_manifest_edit(tmp_path: Path) -> None:
    """Section 15's exit test for v2, run against the real model.

    The two runs differ in one thing a person edited: which manifest implements
    `extract@1`. Neither run touches the loop, the ladder or the ledger writer.

    What is asserted is the seam and not the answer. The model gets fields wrong
    at this prompt, and it may fail validation and be retried; both outcomes go
    through the same six phases and both are recorded. A test that demanded the
    right answer here would be measuring the model, which is what the fixture
    set and the ledger are for.
    """
    code_run, code_outcome = run_with(tmp_path, MANIFESTS, "job-code")
    assert code_outcome.ok
    code_facts = [e for e in code_run.ledger.events() if e["type"] == "fact_added"]
    assert [e["provider"] for e in code_facts] == ["code-logsummary"]

    swapped = [m for m in MANIFESTS if m["id"] != "code-logsummary"] + [MANIFEST]
    model_run, _ = run_with(tmp_path, swapped, "job-model", adapters=[an_adapter()])

    events = model_run.ledger.events()
    named = [e for e in events if e.get("provider") == "extract-350m-q4"]
    assert named, "the model provider never ran, so nothing was swapped"
    assert all(e["type"] in {"fact_added", "validation_failed"} for e in named)
    # Every call is priced, whether it produced a fact or a failure. Section
    # 12.4: a failed call still cost the time, and the cost model reads this.
    assert all(e["duration_ms"] > 0 for e in named)


def test_the_model_output_is_json_that_fits_the_schema(tmp_path: Path) -> None:
    """The grammar is what makes this true, and it is worth asserting once.

    Section 11.2 rests on the output space being narrow, not on the model being
    well behaved. So: whatever the model said, it parsed and it had the seven
    fields the schema declares.
    """
    adapter = an_adapter()
    handle = adapter.load(MANIFEST)
    try:
        from picoharness.payload import text

        body = (REPO / "fixtures" / "extract" / "normal-01-syslog-disk-io.input").read_text(
            encoding="utf-8"
        )
        record = adapter.run(handle, text(body), "log_summary@2")
    finally:
        adapter.unload(handle)

    assert isinstance(record, dict)
    assert set(record) == set(SCHEMAS["log_summary@2"].body["required"])
    assert SCHEMAS["log_summary@2"].check(record).ok
