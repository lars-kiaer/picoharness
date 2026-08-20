"""Capabilities, providers, and the selection between them. Sections 6 and 7.1.

The system has no first use case, so the design must not name its models. It
names **capabilities**. A model, a script or a parser can supply one, and the
runtime does not care which.

A capability needs three roles, and one alone is not a capability:

* **Definition** — the name, the version, and the two schemas.
* **Provider** — something that satisfies the contract.
* **Consumer** — a tool or a phase that actually asks for it.

A definition with no consumer is a guess about the future. Delete it.

## What `select()` does and does not do yet

Section 6.4 gives the full policy. This implements the filters that v1 can
honestly apply — modality, trust ceiling, control permission — and the sort that
matters most: `kind == "code"` first. The two filters that need measurements,
quality floor and estimated cost against the remaining budget, attach at the
marked points once `v_provider_health` and `v_provider_cost` have rows in them.

That ordering is deliberate. A cost filter with no measurements would be a
filter on guesses, and section 12.4 is clear that a manifest number is a guess
by the following week.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapters.base import Adapter, ProviderError
from .trust import TRUST_ORDER


@dataclass(frozen=True, slots=True)
class Capability:
    """A named, versioned contract. Section 6.1."""

    name: str
    input_schema: str
    output_schema: str
    is_control: bool = False

    @property
    def id(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Provider:
    """One manifest, read. Section 6.3."""

    id: str
    kind: str
    implements: tuple[str, ...]
    manifest: dict[str, Any] = field(default_factory=dict)

    def max_trust_in(self, capability: str | None = None) -> str:
        """The highest trust level this provider may read, for one capability.

        Section 11.2 constrains what a **call** has read, not what a provider's
        code is. The same weights can serve `extract@1` over a poisoned log and
        `route@1` over a typed goal, as long as one call never mixes T1 input
        with control output. So the ceiling is declared per capability:

            "security": { "max_trust_in": { "extract@1": "T1", "route@1": "T0" } }

        A capability the manifest does not list gets `T2`, the strictest answer.
        Silence must not widen a security boundary.

        The scalar form is still accepted and applies to every capability.
        """
        declared = self.manifest.get("security", {}).get("max_trust_in", "T1")
        if isinstance(declared, dict):
            return declared.get(capability, "T2")
        return declared

    def may_emit_control(self, capability: str | None = None) -> bool:
        """May this provider decide what happens next, under this capability?

        Declared as a list of the capabilities it is allowed to control with, so
        that one provider can serve a data capability and a control capability
        without the two permissions collapsing into one flag:

            "security": { "may_emit_control": ["route@1"] }
        """
        declared = self.manifest.get("security", {}).get("may_emit_control", False)
        if isinstance(declared, (list, tuple, set)):
            return capability in declared
        return bool(declared)

    @property
    def produces(self) -> tuple[str, ...]:
        return tuple(self.manifest.get("produces", ()))

    @property
    def escalates_to(self) -> str | None:
        return self.manifest.get("escalates_to")

    def accepts(self, mime: str) -> bool:
        declared = self.manifest.get("modality_in")
        if not declared:
            return True
        family = mime.split("/", 1)[0]
        return family in declared or mime in declared


class CapabilityGap(ProviderError):
    """No installed provider can satisfy a request. Section 6.6.

    This is a valid outcome, not a crash. The runtime writes a `capability_gap`
    event, and after a month the gap log says which specialist model to install
    next — so the system tells you what it needs instead of you guessing in
    advance which models to collect.
    """


class Registry:
    """Capabilities, providers, and the adapters that can run them."""

    def __init__(self) -> None:
        self.capabilities: dict[str, Capability] = {}
        self.providers: dict[str, Provider] = {}
        self.adapters: dict[str, Adapter] = {}
        self._by_capability: dict[str, list[str]] = {}

    # -- registration ------------------------------------------------------

    def add_capability(self, capability: Capability) -> Registry:
        self.capabilities[capability.id] = capability
        return self

    def add_adapter(self, adapter: Adapter) -> Registry:
        self.adapters[adapter.kind] = adapter
        return self

    def add_provider(self, manifest: dict[str, Any]) -> Provider:
        for required in ("id", "kind", "implements"):
            if required not in manifest:
                raise ProviderError(f"manifest is missing {required!r}: {manifest}")
        provider = Provider(
            id=manifest["id"],
            kind=manifest["kind"],
            implements=tuple(manifest["implements"]),
            manifest=manifest,
        )
        self.providers[provider.id] = provider
        for capability in provider.implements:
            self._by_capability.setdefault(capability, []).append(provider.id)
        return provider

    def load_dir(self, directory: str | Path, pattern: str = "*.manifest.json") -> list[Provider]:
        """Read every manifest under a directory. Nothing is set in code."""
        found = []
        for path in sorted(Path(directory).glob(pattern)):
            found.append(self.add_provider(json.loads(path.read_text(encoding="utf-8"))))
        return found

    # -- selection, section 6.4 -------------------------------------------

    def candidates(self, capability: str) -> list[Provider]:
        return [self.providers[pid] for pid in self._by_capability.get(capability, ())]

    def select(
        self,
        capability: str,
        *,
        schema: str | None = None,
        trust_in: str = "T1",
        mime: str = "text/plain",
        exclude: frozenset[str] = frozenset(),
    ) -> Provider:
        """Choose a provider, deterministically. Raises `CapabilityGap` if none.

        The two lines that carry most of the value are the trust filter, which
        enforces the security invariant of section 11.2 in one place, and the
        `kind == "code"` sort, which means the system gets faster and more
        reliable every time a model is replaced by a parser.
        """
        pool = [p for p in self.candidates(capability) if p.id not in exclude]
        if not pool:
            raise CapabilityGap(
                f"no provider implements {capability!r}"
                + (f" outside {sorted(exclude)}" if exclude else "")
            )

        definition = self.capabilities.get(capability)
        is_control = bool(definition and definition.is_control)

        # A capability names a contract, not an output. Two providers can both
        # implement `extract@1` and produce different schemas, so the target
        # schema is part of the request. A provider that declares no `produces`
        # is taken at its word and stays in the pool.
        if schema:
            pool = [p for p in pool if not p.produces or schema in p.produces]

        pool = [p for p in pool if p.accepts(mime)]
        pool = [p for p in pool if TRUST_ORDER[trust_in] <= TRUST_ORDER[p.max_trust_in(capability)]]
        pool = [p for p in pool if p.may_emit_control(capability) == is_control]
        pool = [p for p in pool if p.kind in self.adapters]

        # Section 6.4 also filters on measured pass rate against a quality floor,
        # and on estimated cost against the remaining budget. Both attach here,
        # once `v_provider_health` and `v_provider_cost` have rows. Filtering on
        # a manifest guess before then would be worse than not filtering.

        if not pool:
            raise CapabilityGap(
                f"{capability!r} has providers, but none produces {schema or 'any schema'} "
                f"from {trust_in} {mime} as a "
                f"{'control' if is_control else 'data'} capability"
            )

        pool.sort(key=lambda p: (p.kind != "code", p.id))
        return pool[0]

    def adapter_for(self, provider: Provider) -> Adapter:
        try:
            return self.adapters[provider.kind]
        except KeyError as exc:
            raise ProviderError(f"no adapter for kind {provider.kind!r}") from exc

    # -- the composition document ------------------------------------------

    def resolve(self) -> dict[str, Any]:
        """Everything registered, for the composition hash of section 4.6."""
        return {
            "capabilities": {
                name: {
                    "input": c.input_schema,
                    "output": c.output_schema,
                    "control": c.is_control,
                }
                for name, c in sorted(self.capabilities.items())
            },
            "providers": {pid: p.manifest for pid, p in sorted(self.providers.items())},
            "adapters": sorted(self.adapters),
        }


__all__ = ["Registry", "Provider", "Capability", "CapabilityGap"]
