"""An instruction is a typed record with one free-text field. Section 10.3.

The system prompt and the schema describe the same rules in two files. Hashed
apart, they drift apart while both stay pinned, and two independent pins on one
rule are a source of drift and not two defences. So they become one artefact:
many typed fields, and exactly one field of free text.

Four rules make it work, and this module enforces three of them.

**The prose completes the schema. It does not repeat it.** Field rules stay in
the schema descriptions and are rendered in where the prose puts `<<schema>>`,
so a change to a field rule happens in one place. What the prose carries is what
no schema keyword holds: ordering, counting conventions, tone, and a worked
example.

**The free text is written at build time and never at run time.** A planner has
nowhere to put an instruction, because the only run-time inputs are the declared
parameters. That is a boundary made of shape, not a rule somebody must remember.

**A parameter is typed, trusted, and closed where it can be.** A type alone is
not enough: a `string` parameter is prose-shaped, so the type only renames the
injection. A value space is closed wherever there is one, and `render()` refuses
a parameter that is not `T0`. Note what that does and does not do. It refuses a
parameter the artefact *declares* as tainted; it cannot see where the caller got
a value it passes into a `T0` parameter. Trust of the value is the runtime's
book-keeping, in `trust.py`. This is the declaration being held to.

The fourth rule is the format. JSON escapes a newline, which would make the one
artefact you most need to read in a diff the one you cannot. TOML front matter
with a prose body keeps the metadata checkable and the diff legible, and
`tomllib` is in the standard library, so it costs no dependency.

`<<name>>` is the substitution syntax rather than `{name}`, because a worked
example in the prose is usually JSON, and JSON is made of braces.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .validate import Schema

FENCE = "+++"
TOKEN = re.compile(r"<<([a-z_][a-z0-9_]*)>>")

#: The front matter, section 10.3. Held to a schema for the reason in
#: `schemas.py`: a key nobody declared is a key nobody checked.
INSTRUCTION_SCHEMA: dict[str, Any] = {
    "title": "instruction@1",
    "type": "object",
    "additionalProperties": False,
    "required": ["capability", "schema", "covers"],
    "properties": {
        "capability": {"type": "string", "minLength": 1},
        "schema": {"type": "string", "minLength": 1},
        "covers": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "type"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "type": {
                        "type": "string",
                        "enum": ["string", "integer", "number", "boolean"],
                    },
                    "trust": {"type": "string", "enum": ["T0", "T1", "T2"]},
                    "values": {"type": "array"},
                },
            },
        },
    },
}

_INSTRUCTION = Schema(schema_id="instruction@1", body=INSTRUCTION_SCHEMA)
_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


class InstructionError(ValueError):
    """The artefact is wrong. A build-time fault, never a run-time one."""


@dataclass(frozen=True, slots=True)
class Parameter:
    """One value a caller may supply. Everything else about the prompt is fixed."""

    name: str
    type: str
    trust: str = "T0"
    values: tuple[Any, ...] = ()

    def refuse(self, value: Any) -> str | None:
        """Why this value may not be rendered, or None."""
        if self.trust != "T0":
            return f"{self.name} is declared {self.trust}, and only T0 may enter a pinned prompt"
        wanted = _TYPES[self.type]
        # `bool` is an `int` in Python, and an accidental True where a count
        # belongs would render as "True". The type says what it says.
        if isinstance(value, bool) != (self.type == "boolean") or not isinstance(value, wanted):
            return f"{self.name} takes {self.type}, not {type(value).__name__}"
        if self.values and value not in self.values:
            return f"{self.name} takes one of {list(self.values)}, not {value!r}"
        return None


@dataclass(frozen=True, slots=True)
class Instruction:
    """One prompt envelope: the typed fields, the prose, and one hash over both."""

    capability: str
    schema: str
    covers: tuple[str, ...]
    prose: str
    parameters: tuple[Parameter, ...] = ()
    source: str = field(default="", repr=False)

    @classmethod
    def parse(cls, text: str) -> Instruction:
        head, sep, body = text.partition(f"{FENCE}\n")
        if head.strip() or not sep:
            raise InstructionError(f"an instruction must open with a {FENCE} fence")
        front, sep, prose = body.partition(f"{FENCE}\n")
        if not sep:
            raise InstructionError(f"the front matter is not closed by {FENCE}")
        try:
            fields = tomllib.loads(front)
        except tomllib.TOMLDecodeError as exc:
            raise InstructionError(f"the front matter is not TOML: {exc}") from exc

        result = _INSTRUCTION.check(fields)
        if not result.ok:
            raise InstructionError(f"the front matter is not an instruction@1: {result.error()}")
        return cls(
            capability=fields["capability"],
            schema=fields["schema"],
            covers=tuple(fields["covers"]),
            prose=prose.strip() + "\n",
            parameters=tuple(
                Parameter(
                    name=p["name"],
                    type=p["type"],
                    trust=p.get("trust", "T0"),
                    values=tuple(p.get("values", ())),
                )
                for p in fields.get("parameters", ())
            ),
            source=text,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> Instruction:
        return cls.parse(Path(path).read_text(encoding="utf-8"))

    def digest(self) -> str:
        """One hash over the whole envelope, so the pin list gets shorter."""
        return "sha256:" + hashlib.sha256(self.source.encode("utf-8")).hexdigest()

    def audit(self, schema: Schema) -> list[str]:
        """Everything checkable at build time. Section 6.1, applied to a prompt."""
        problems: list[str] = []
        if schema.schema_id != self.schema:
            problems.append(f"declares schema {self.schema!r} but was given {schema.schema_id!r}")
        properties = set(schema.body.get("properties", {}))
        for name in self.covers:
            if name not in properties:
                problems.append(f"covers {name!r}, which {schema.schema_id} does not declare")
        for name in schema.body.get("required", ()):
            if name not in self.covers:
                problems.append(
                    f"{name!r} is required by the schema and the prose does not cover it"
                )

        used = set(TOKEN.findall(self.prose))
        if self.prose.count("<<schema>>") != 1:
            problems.append("the prose must place <<schema>> exactly once")
        declared = {p.name for p in self.parameters}
        for name in sorted(used - declared - {"schema"}):
            problems.append(f"the prose uses <<{name}>>, which no parameter declares")
        for name in sorted(declared - used):
            problems.append(f"parameter {name!r} is declared and never rendered")
        return problems

    def render(self, schema: Schema, **values: Any) -> str:
        """The prompt as the provider sees it. Build time decided everything else."""
        by_name = {p.name: p for p in self.parameters}
        for name, value in values.items():
            parameter = by_name.get(name)
            if parameter is None:
                raise InstructionError(f"{name!r} is not a parameter of this instruction")
            if (refused := parameter.refuse(value)) is not None:
                raise InstructionError(refused)
        missing = sorted(by_name.keys() - values.keys())
        if missing:
            raise InstructionError(f"no value for parameter(s) {missing}")

        filled = {"schema": render_schema(schema), **{k: str(v) for k, v in values.items()}}
        return TOKEN.sub(lambda m: filled[m.group(1)], self.prose)


def render_schema(schema: Schema) -> str:
    """The schema as the model sees it: every rule, none of the plumbing.

    `$schema` and `$id` address the file to a validator and cost tokens the
    model has no use for. Everything else stays, because the descriptions are
    the field rules and dropping one would put that rule back in the prose.
    """
    body = {k: v for k, v in schema.body.items() if not k.startswith("$")}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "Instruction",
    "Parameter",
    "InstructionError",
    "INSTRUCTION_SCHEMA",
    "render_schema",
    "FENCE",
]
