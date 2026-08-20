"""The schemas the runtime holds itself to.

The data plane has been checked from the start: everything a provider produces
passes a schema, a range check and a cross-check. The configuration plane was
not checked at all, and that is the wrong way round — a mistake in a manifest
decides how every later call behaves.

The failure that made this urgent is small and quiet. Write `secuirty` instead
of `security` and the whole block is missed, so `max_trust_in` falls back to its
default of `T1`. A misspelling therefore makes a security boundary **wider**,
which is the exact inverse of the rule in section 6.2 that silence must never do
that. `additionalProperties: false` turns that typo into an error at load.

These live in Python and not in a file on purpose. They are the fixed point:
nothing validates them, in the way that nothing compiles a compiler. A file
could drift from the code that reads it, so they version with the code instead,
and they go into the composition hash like everything else.
"""

from __future__ import annotations

from typing import Any

TRUST = ["T0", "T1", "T2"]

#: A provider manifest, section 6.3.
MANIFEST_SCHEMA: dict[str, Any] = {
    "title": "manifest@1",
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "implements"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["code", "gguf", "onnx", "binary"]},
        "implements": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "produces": {"type": "array", "items": {"type": "string", "minLength": 1}},
        # How the adapter reaches it. Which one applies depends on `kind`, and
        # the adapter says so at load; the schema only fixes the shape.
        "entrypoint": {"type": ["string", "null"]},
        "file": {"type": ["string", "null"]},
        "sha256": {"type": ["string", "null"]},
        "modality_in": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "languages": {"type": "array", "items": {"type": "string"}},
        "max_input_bytes": {"type": ["integer", "null"], "minimum": 0},
        # Filled by `probe()`, not by hand. Section 12 and 12.4.
        "resource": {"type": "object"},
        "cost": {"type": "object"},
        "security": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                # Either one level for every capability, or one per capability.
                # Section 6.2: the boundary belongs to the capability, because
                # 11.2 constrains what a call has read and not what the weights
                # are.
                # No `enum` here. The permitted values depend on the shape:
                # a string is one level, a mapping is one level per capability.
                # A rule that spans two fields belongs in code, not in a schema —
                # `validate_security_block` checks both forms.
                "max_trust_in": {"type": ["string", "object"]},
                "may_emit_control": {"type": ["boolean", "array"]},
            },
        },
        "confidence_floor": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "determinism": {"type": "string", "enum": ["exact", "seeded", "none"]},
        "sampler": {"type": "object"},
        "grammar": {"type": ["string", "null"]},
        "system_prompt": {"type": ["string", "null"]},
        "escalates_to": {"type": ["string", "null"]},
        "probe_sample": {"type": ["string", "null"]},
    },
}

#: A tool manifest, section 8.1.
TOOL_SCHEMA: dict[str, Any] = {
    "title": "tool@1",
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "reducer", "output_schema", "effect", "trust_out"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "description": {"type": ["string", "null"]},
        "input_schema": {"type": ["string", "null"]},
        "output_schema": {"type": "string", "minLength": 1},
        "reducer": {"type": "string", "minLength": 1},
        "effect": {"type": "string", "enum": ["read_only", "write", "destructive"]},
        "idempotent": {"type": "boolean"},
        "trust_out": {"type": "string", "enum": TRUST},
        "sandbox": {"type": ["string", "null"]},
        "version": {"type": "integer", "minimum": 1},
    },
}


#: The arguments a path-reading tool takes. Section 8.1 declares an
#: `input_schema` for every tool; this is the first one.
#:
#: The plan is the control plane, and from v4 a model writes it. Checking the
#: arguments is therefore not book-keeping: it is the only schema between a
#: planner and executable code.
READ_PATH_SCHEMA: dict[str, Any] = {
    "title": "read_path@1",
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {"path": {"type": "string", "minLength": 1}},
}


def _trust_of(security: Any, capability: str | None) -> str:
    """Read a ceiling that may be a scalar or a mapping. Used by the checks."""
    declared = (security or {}).get("max_trust_in", "T1")
    if isinstance(declared, dict):
        return declared.get(capability, "T2")
    return declared


def validate_security_block(manifest: dict[str, Any]) -> list[str]:
    """Checks the schema cannot express, because they span two fields.

    A per-capability mapping must name capabilities the provider implements, and
    every level must be one of the three. A mapping that names `extarct@1` would
    otherwise be silently ignored, and the real capability would fall through to
    the strict default — which is safe, but silently wrong is still wrong.
    """
    problems: list[str] = []
    security = manifest.get("security") or {}
    implements = set(manifest.get("implements", ()))

    declared = security.get("max_trust_in", "T1")
    levels = declared.values() if isinstance(declared, dict) else [declared]
    for level in levels:
        if level not in TRUST:
            problems.append(f"max_trust_in has {level!r}; expected one of {TRUST}")
    if isinstance(declared, dict):
        for name in declared:
            if name not in implements:
                problems.append(
                    f"max_trust_in names {name!r}, which this provider does not implement"
                )

    control = security.get("may_emit_control", False)
    if isinstance(control, (list, tuple)):
        for name in control:
            if name not in implements:
                problems.append(
                    f"may_emit_control names {name!r}, which this provider does not implement"
                )
    return problems


__all__ = [
    "MANIFEST_SCHEMA",
    "TOOL_SCHEMA",
    "READ_PATH_SCHEMA",
    "TRUST",
    "validate_security_block",
]
