"""A `code` provider for `answer@1`.

The final answer is a rendering of validated facts, so v1 renders it with a
template. That is not a placeholder for a model — it is the honest form for a
single expert user, and section 16 question 6 has to be settled before anything
more is worth building.

The rule this enforces is section 5.3's: a partial answer with a named gap beats
a timeout. When a step failed, the gap is stated rather than papered over.
"""

from __future__ import annotations

import json

from ..payload import Payload


def render(payload: Payload) -> str:
    """Turn `{goal, facts, missing}` into text for the user."""
    request = json.loads(payload.as_text())
    lines = [f"Goal: {request['goal']}", ""]

    facts = request.get("facts") or []
    if facts:
        lines.append(f"Found {len(facts)} fact(s):")
        for fact in facts:
            stated = ", ".join(f"{k}={v!r}" for k, v in sorted(fact.items()) if v is not None)
            absent = sorted(k for k, v in fact.items() if v is None)
            lines.append(f"  - {stated}")
            if absent:
                # Naming what the input did not contain is the whole point of
                # abstention. A field that is simply left out reads as an
                # oversight; a field reported absent reads as an observation.
                lines.append(f"    not present in the input: {', '.join(absent)}")
    else:
        lines.append("No facts were collected.")

    missing = request.get("missing") or []
    if missing:
        lines += ["", f"Incomplete. These steps did not produce a fact: {', '.join(missing)}."]
    return "\n".join(lines)


__all__ = ["render"]
