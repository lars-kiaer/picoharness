"""The validation ladder, section 10.2.

Apply the cheap checks first. Stop at the first failure.

| Level | Check | Cost | Catches |
|-------|-------|------|---------|
| 1 | Grammar | 0 | Invalid syntax |
| 2 | Schema | ~1 ms | Missing or wrong-typed fields |
| 3 | Range and unit | ~1 ms | A percentage above 100 |
| 4 | Deterministic cross-check | ~10 ms | A count that `grep -c` disagrees with |
| 5 | Semantic critic | ~1 s | Wrong meaning |
| 6 | Human | minutes | Everything else |

Levels 2, 3 and 4 are here. Level 1 belongs to the provider: a grammar makes
invalid output impossible to emit, so there is nothing to check afterwards.
Levels 5 and 6 are steps in a plan, not functions.

**Level 4 is the level that most designs skip**, and section 10.2 says it is
also the cheapest real defence against the failure in 10.1: a model that writes
perfect JSON holding wrong numbers. If a model counts errors in a log, count
them again with code and compare. If the two disagree, trust the code.

The result names the level that caught the problem, using the same words as the
failure taxonomy in `picoharness.memory.failure`. That is not a coincidence: a
failure record says which level caught it, which tells you whether your cheap
checks are doing their job.

No third-party dependencies. Section 14 suggests Pydantic; the schemas this
system uses are small enough that the standard library covers them, and every
dependency is one more thing to provision by hand on an offline box.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Level number to the word the failure taxonomy uses.
LEVEL_KIND: dict[int, str] = {
    1: "grammar",
    2: "schema",
    3: "range",
    4: "crosscheck",
    5: "semantic",
}

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


class SchemaError(ValueError):
    """The schema itself is wrong. This is a bug in the repository, not in a run."""


@dataclass(frozen=True, slots=True)
class Failure:
    """One reason an instance was rejected."""

    level: int
    path: str
    message: str

    @property
    def kind(self) -> str:
        """The word the ledger and the failure taxonomy use."""
        return LEVEL_KIND[self.level]

    def __str__(self) -> str:
        where = self.path or "<root>"
        return f"{where}: {self.message}"


@dataclass(frozen=True, slots=True)
class Result:
    """What the ladder decided, and which rung caught it."""

    ok: bool
    failures: tuple[Failure, ...] = ()

    @property
    def level(self) -> int | None:
        """The rung that caught it. The lowest one, because we stop at the first."""
        return min((f.level for f in self.failures), default=None)

    @property
    def kind(self) -> str | None:
        """Ready to write into a `validation_failed` event."""
        return LEVEL_KIND[self.level] if self.level is not None else None

    def error(self) -> str:
        """One line, for the ledger. Detail text can be tainted; see 9.7."""
        return "; ".join(str(f) for f in self.failures)

    def __bool__(self) -> bool:
        return self.ok


OK = Result(ok=True)


# --------------------------------------------------------------------------
# levels 2 and 3
# --------------------------------------------------------------------------


def _type_matches(value: Any, name: str) -> bool:
    expected = _JSON_TYPES.get(name)
    if expected is None:
        raise SchemaError(f"unknown type {name!r} in schema")
    # JSON keeps booleans and integers apart. Python does not: `bool` is a
    # subclass of `int`, so `True` would pass an integer check. Reject it.
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, expected)


@dataclass(frozen=True, slots=True)
class Schema:
    """A JSON Schema, limited to the keywords this system actually uses.

    Supported: `type` (single or list), `properties`, `required`,
    `additionalProperties`, `enum`, `minimum`, `maximum`, `minLength`,
    `maxLength`, `items`. Anything else in a schema file is a mistake, and
    `SchemaError` says so rather than ignoring it quietly.
    """

    schema_id: str
    body: dict[str, Any]

    KNOWN = frozenset(
        {
            "$schema", "$id", "title", "description", "type", "properties",
            "required", "additionalProperties", "enum", "minimum", "maximum",
            "minLength", "maxLength", "items", "x-extractive",
        }
    )

    @classmethod
    def from_file(cls, path: str | Path, *, schema_id: str | None = None) -> Schema:
        target = Path(path)
        body = json.loads(target.read_text(encoding="utf-8"))
        return cls(schema_id=schema_id or body.get("title") or target.stem, body=body)

    def __post_init__(self) -> None:
        self._audit(self.body, "")

    def _audit(self, node: dict[str, Any], path: str) -> None:
        """Refuse a schema that uses a keyword this validator ignores.

        Silently ignoring `pattern` would mean a schema that looks stricter than
        it is, which is worse than not having the keyword at all.
        """
        unknown = set(node) - self.KNOWN
        if unknown:
            raise SchemaError(
                f"{path or '<root>'}: unsupported schema keyword(s) {sorted(unknown)}. "
                f"Add support in validate.py, or the schema is not what it claims."
            )
        for name, child in node.get("properties", {}).items():
            self._audit(child, f"{path}.{name}" if path else name)
        if isinstance(node.get("items"), dict):
            self._audit(node["items"], f"{path}[]")

    # -- the two rungs -----------------------------------------------------

    def check(self, instance: Any) -> Result:
        """Levels 2 and 3, in one pass, reporting the lower level first."""
        failures: list[Failure] = []
        self._check(instance, self.body, "", failures)
        if not failures:
            return OK
        lowest = min(f.level for f in failures)
        return Result(ok=False, failures=tuple(f for f in failures if f.level == lowest))

    def _check(self, value: Any, node: dict[str, Any], path: str, out: list[Failure]) -> None:
        declared = node.get("type")
        if declared is not None:
            names = declared if isinstance(declared, list) else [declared]
            if not any(_type_matches(value, n) for n in names):
                out.append(Failure(2, path, f"{value!r} is not {' or '.join(names)}"))
                return

        if "enum" in node and value not in node["enum"]:
            out.append(Failure(2, path, f"{value!r} is not one of {node['enum']}"))
            return

        if isinstance(value, dict):
            for name in node.get("required", []):
                if name not in value:
                    out.append(Failure(2, f"{path}.{name}" if path else name, "is required"))
            props = node.get("properties", {})
            if node.get("additionalProperties") is False:
                for name in value:
                    if name not in props:
                        out.append(
                            Failure(2, f"{path}.{name}" if path else name, "is not in the schema")
                        )
            for name, child in props.items():
                if name in value:
                    self._check(value[name], child, f"{path}.{name}" if path else name, out)

        if isinstance(value, list) and isinstance(node.get("items"), dict):
            for i, item in enumerate(value):
                self._check(item, node["items"], f"{path}[{i}]", out)

        # Level 3. A percentage above 100 is well-formed and still wrong.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in node and value < node["minimum"]:
                out.append(Failure(3, path, f"{value} is below the minimum {node['minimum']}"))
            if "maximum" in node and value > node["maximum"]:
                out.append(Failure(3, path, f"{value} is above the maximum {node['maximum']}"))
        if isinstance(value, str):
            if "minLength" in node and len(value) < node["minLength"]:
                out.append(Failure(3, path, f"is shorter than {node['minLength']}"))
            if "maxLength" in node and len(value) > node["maxLength"]:
                out.append(Failure(3, path, f"is longer than {node['maxLength']}"))


# --------------------------------------------------------------------------
# level 4
# --------------------------------------------------------------------------

#: A second, deterministic path to one field. It reads the raw input and
#: returns the value that field must hold, or None when it cannot say.
CrossCheck = Callable[[str], Any]


@dataclass(slots=True)
class CrossChecks:
    """Independent code paths to individual fields. Section 10.2 level 4.

    This is double programming, the same method as in regulated clinical work:
    an independent path must reach the same result. Here the second path is
    deterministic code, so when the two disagree, the code wins.

    Two kinds of check live here. A `CrossCheck` recomputes a value and compares.
    A **span** requirement is weaker and cheaper: it only asks whether the value
    the provider produced actually appears in the input it was given.
    """

    checks: dict[str, CrossCheck] = field(default_factory=dict)
    spans: set[str] = field(default_factory=set)

    def add(self, field_name: str, check: CrossCheck) -> CrossChecks:
        self.checks[field_name] = check
        return self

    def require_span(self, *field_names: str) -> CrossChecks:
        """Demand that these values be copied from the input, not composed.

        Abstention is a contract, not a hope. A field the schema calls
        extractive has exactly one honest source — the input — so a value with
        no span in the input was invented, whatever else is true about it.

        This lands at level 4 and not level 3 on purpose. Level 3 checks a value
        against itself and needs nothing else; this needs the source, which is
        what makes it a second deterministic path. The distinction is not
        bookkeeping: section 2.1 reads `crosscheck` as the dangerous kind,
        because it means the provider lied convincingly. An invented host name
        is exactly that, and filing it as `range` would understate it.
        """
        self.spans.update(field_names)
        return self

    def run(self, instance: dict[str, Any], raw_input: str) -> Result:
        """Compare each checked field against its second path.

        A field the model set to `null` is skipped. `null` is a claim that the
        input does not contain the value, and section 3.1 of the evaluation note
        makes that claim the property we most want a model to be able to make.
        Cross-checking it against a count would report a failure every time
        abstention was correct.

        A check that returns `None` is also skipped: it means the second path
        could not answer either, and two silences do not make a disagreement.
        """
        failures: list[Failure] = []
        for name, check in self.checks.items():
            if name not in instance or instance[name] is None:
                continue
            expected = check(raw_input)
            if expected is None:
                continue
            if instance[name] != expected:
                failures.append(
                    Failure(
                        4,
                        name,
                        f"the model says {instance[name]!r}, code says {expected!r}",
                    )
                )

        for name in sorted(self.spans):
            value = instance.get(name)
            # A null is the honest answer for an absent field, so it is never a
            # span failure. Only a non-null value has something to justify.
            if not isinstance(value, str) or not value:
                continue
            if value not in raw_input:
                failures.append(
                    Failure(4, name, f"{value!r} does not appear in the input it was given")
                )

        return Result(ok=not failures, failures=tuple(failures))


# --------------------------------------------------------------------------
# the ladder
# --------------------------------------------------------------------------


def ladder(
    instance: Any,
    schema: Schema,
    *,
    raw_input: str | None = None,
    cross_checks: CrossChecks | None = None,
) -> Result:
    """Run the rungs in order and stop at the first failure.

    Order is the whole design. A schema check costs about a millisecond and a
    critic model costs about a second, so a wrong type must never reach the
    expensive rung.
    """
    result = schema.check(instance)
    if not result.ok:
        return result
    if cross_checks is not None and raw_input is not None and isinstance(instance, dict):
        return cross_checks.run(instance, raw_input)
    return OK


def extractive_fields(schema: Schema) -> tuple[str, ...]:
    """The fields a schema says are copied from the input, in order."""
    return tuple(
        name
        for name, spec in schema.body.get("properties", {}).items()
        if spec.get("x-extractive")
    )


def spans_for(schema: Schema) -> CrossChecks:
    """Cross-checks that hold every extractive field to its source."""
    return CrossChecks().require_span(*extractive_fields(schema))


def count_matches(pattern: str, *, flags: int = 0) -> CrossCheck:
    """A cross-check that counts matching lines. The `grep -c` of section 10.2."""
    import re

    compiled = re.compile(pattern, flags)
    return lambda raw: sum(1 for line in raw.splitlines() if compiled.search(line))


__all__ = [
    "Schema",
    "extractive_fields",
    "spans_for",
    "SchemaError",
    "CrossChecks",
    "CrossCheck",
    "Result",
    "Failure",
    "LEVEL_KIND",
    "ladder",
    "count_matches",
    "OK",
]
