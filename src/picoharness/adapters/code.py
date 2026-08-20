"""The `code` adapter. Write this one first. Section 7.1.

A `code` provider is deterministic Python: a regular expression, a parser, a
small classifier. It satisfies a capability contract exactly like a model does,
and the selection policy of section 6.4 prefers it — `kind == "code"` sorts
first, so the system gets faster and more reliable every time a model is
replaced by a parser.

This is principle P3 made structural rather than advisory. It is also why v1 of
the build plan has no model in it: a system that works with only a parser proves
that the ledger, the loop and the validation are correct. Add a model first and
you will not know which layer is wrong.

A manifest for a code provider names an entry point:

```json
{
  "id": "code-logsummary",
  "implements": ["extract@1"],
  "kind": "code",
  "entrypoint": "picoharness.providers.log_summary:extract",
  "produces": ["log_summary@2"],
  "security": { "max_trust_in": "T1", "may_emit_control": false },
  "determinism": "exact"
}
```

The entry point is a plain function of one payload. It returns a record, or
raises `ProviderError` when it cannot. It must not read the clock, the network,
or anything outside its argument — the whole reproducibility contract of
section 10.3 rests on a step being a pure function.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from typing import Any

from ..payload import Payload
from .base import Handle, ProviderError, Scope, timed_probe

#: What a code provider's entry point looks like.
Entrypoint = Callable[[Payload], Any]


def resolve(target: str) -> Entrypoint:
    """Turn `"module.path:function"` into the function.

    Errors here are configuration errors, not run-time errors, and they say so.
    A manifest that points at nothing should fail at load, loudly, rather than
    at the first step that needs it.
    """
    if ":" not in target:
        raise ProviderError(
            f"entrypoint {target!r} must be 'module.path:function'"
        )
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ProviderError(f"cannot import {module_name!r} for entrypoint {target!r}") from exc
    try:
        fn = getattr(module, attribute)
    except AttributeError as exc:
        raise ProviderError(f"{module_name!r} has no attribute {attribute!r}") from exc
    if not callable(fn):
        raise ProviderError(f"{target!r} is not callable")
    return fn


class CodeAdapter:
    """Runs deterministic Python providers.

    It holds no state between calls. The scope exists anyway, because the
    interface is the same for every kind and an adapter that skips it teaches
    the wrong shape to whoever writes the `gguf` one.
    """

    kind = "code"

    def load(self, manifest: dict[str, Any]) -> Handle:
        entry = manifest.get("entrypoint")
        if not entry:
            raise ProviderError(f"{manifest.get('id', '?')} has no entrypoint")
        fn = resolve(entry)
        scope = Scope()

        # A code provider imports a module, and an import is a registration:
        # the module stays in `sys.modules` and the reference stays reachable.
        # Dropping the reference is all this one owes, but it owes it through
        # the scope like everything else, so the shape is right when a `gguf`
        # adapter arrives with a KV snapshot and a thread pool to give back.
        holder: dict[str, Any] = {"fn": fn}
        scope.register(f"entrypoint {entry}", lambda: holder.clear())

        return Handle(
            provider_id=manifest.get("id", entry),
            scope=scope,
            obj=holder,
            meta={"entrypoint": entry, "produces": manifest.get("produces", [])},
        )

    def run(self, handle: Handle, payload: Payload, schema_id: str) -> Any:
        holder = handle.obj
        if not holder or "fn" not in holder:
            raise ProviderError(f"{handle.provider_id} was unloaded; load it again")
        try:
            return holder["fn"](payload)
        except ProviderError:
            raise
        except Exception as exc:
            # A provider that throws is a failure of the provider, not of the
            # runtime. Naming it keeps the failure taxonomy honest: this becomes
            # `tool_error`, not `unknown`.
            raise ProviderError(f"{handle.provider_id} raised {type(exc).__name__}: {exc}") from exc

    def unload(self, handle: Handle) -> None:
        handle.scope.unwind()

    def probe(self, manifest: dict[str, Any]) -> dict[str, Any]:
        sample = Payload(data=manifest.get("probe_sample", "sample\n"), trust="T2")
        schema = (manifest.get("produces") or ["unknown@0"])[0]
        measured = timed_probe(self, manifest, sample, schema)
        measured["ram_mb"] = 0  # a code provider adds no resident weights
        return measured


def time_call(fn: Callable[[], Any]) -> tuple[Any, float]:
    """Run something and say how long it took, in milliseconds.

    Every provider call writes its duration into the ledger, and section 12.4
    turns those durations into the cost model. A call that is not timed is a
    call the policy cannot price.
    """
    started = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - started) * 1000.0


__all__ = ["CodeAdapter", "resolve", "time_call", "Entrypoint"]
