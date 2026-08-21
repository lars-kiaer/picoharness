"""llama.cpp in this process. Sections 7.1, 7.1.1, and 1.5 of the eval note.

In process, and not `llama-server` over HTTP, because scope unwind needs real
control over load and unload. A subprocess does not give it, and a serial design
that swaps providers many times inside one task leaks through that gap.

This adapter does three things the `code` adapter never has to.

**It checks the weights against the manifest before it loads them.** Section
10.3 pins the weights file by hash. A pin nobody checks is a comment.

**It builds the grammar from the schema.** Section 11.2: narrow the output space
until the wrong answer has no representation. This was measured on the first
call this adapter was written for. Free, the model spent 439 tokens copying the
schema back at itself. With the grammar the schema echo had no production, so
the same model emitted the seven fields in 56 tokens, and took half the time.

**It renders the system prompt from the instruction record of section 10.3**, so
the field rules come from the schema and not from a second copy of them that can
drift.

The payload goes in the user turn and never in the system turn. That is not a
convention here, it is the trust boundary of section 11.2: the system channel
carries what the system knows, and the document is T1.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..payload import Payload
from ..prompt import Instruction
from ..validate import Schema
from .base import Handle, ProviderError, Scope, timed_probe


def file_digest(path: Path) -> str:
    """The hash of a file, read in blocks. `hashlib` has done this since 3.11."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


@dataclass(slots=True)
class GgufAdapter:
    """One adapter for every GGUF provider. The manifest says which model."""

    model_root: Path
    prompt_root: Path
    schemas: dict[str, Schema]
    n_threads: int = 4
    n_ctx: int = 8192
    kind: str = "gguf"
    _checked: dict[tuple[str, int, int], str] = field(default_factory=dict, repr=False)

    # -- the weights -------------------------------------------------------

    def _verify(self, path: Path, declared: str | None) -> None:
        """Hold the file to the hash the manifest pins it by.

        Hashing 220 MB costs real time, and this design loads a provider many
        times inside one task, so the answer is cached against size and mtime.
        A file that changed underneath changes both, and is hashed again.
        """
        if not declared:
            raise ProviderError(f"{path.name}: the manifest pins no sha256, so nothing is pinned")
        try:
            stat = path.stat()
        except OSError as exc:
            raise ProviderError(f"{path}: {exc}") from exc
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        found = self._checked.get(key) or file_digest(path)
        self._checked[key] = found
        if found != declared.removeprefix("sha256:"):
            raise ProviderError(
                f"{path.name} is not the file this manifest pins.\n"
                f"  manifest: {declared}\n  on disk:  sha256:{found}"
            )

    # -- the adapter protocol ---------------------------------------------

    def load(self, manifest: dict[str, Any]) -> Handle:
        try:
            from llama_cpp import Llama
        except ImportError as exc:  # pragma: no cover - platform dependent
            raise ProviderError(
                "the gguf adapter needs llama-cpp-python: pip install '.[gguf]'"
            ) from exc

        path = self.model_root / (manifest.get("file") or "")
        self._verify(path, manifest.get("sha256"))
        instruction = self._instruction(manifest)

        sampler = manifest.get("sampler") or {}
        scope = Scope()
        model = Llama(
            model_path=str(path),
            n_ctx=manifest.get("context") or self.n_ctx,
            n_threads=sampler.get("n_threads") or self.n_threads,
            seed=sampler.get("seed", 0),
            verbose=False,
        )
        # P13. Everything `load()` took must be given back, and the model holds
        # a memory mapping and file descriptors, not only Python objects.
        scope.register(f"llama:{path.name}", model.close)
        return Handle(
            provider_id=manifest["id"],
            scope=scope,
            obj=model,
            meta={"instruction": instruction, "sampler": sampler, "grammars": {}},
        )

    def run(self, handle: Handle, payload: Payload, schema_id: str) -> Any:
        from llama_cpp import LlamaGrammar

        schema = self.schemas.get(schema_id)
        if schema is None:
            raise ProviderError(f"{schema_id!r} is not registered with this adapter")
        cache = handle.meta["grammars"]
        if schema_id not in cache:
            instruction: Instruction = handle.meta["instruction"]
            cache[schema_id] = (
                LlamaGrammar.from_json_schema(json.dumps(schema.body), verbose=False),
                instruction.render(schema),
            )
        grammar, system = cache[schema_id]

        sampler = handle.meta["sampler"]
        answer = handle.obj.create_chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": payload.as_text()},
            ],
            temperature=sampler.get("temp", 0.0),
            seed=sampler.get("seed", 0),
            max_tokens=sampler.get("max_tokens", 512),
            grammar=grammar,
        )
        # A bare record and no confidence, on purpose. Section 6.5: a provider
        # that reports no score is never gated, and an uncalibrated score is
        # worse than none because it looks like information. A number from this
        # model would have to be measured before it may gate anything.
        text = answer["choices"][0]["message"]["content"]
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            # The grammar makes this close to impossible, which is why it is
            # worth reporting loudly rather than retrying.
            raise ProviderError(f"the grammar admitted output that is not JSON: {exc}") from exc

    def unload(self, handle: Handle) -> None:
        handle.scope.unwind()

    def probe(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Fill the `null` cost fields of section 6.3. Run it again after a move.

        The probe sample is a build-time artefact and lives with the prompts,
        not with the weights. It is text that the composition pins, in the same
        way the prompt is.
        """
        sample = manifest.get("probe_sample")
        if not sample:
            raise ProviderError(f"{manifest['id']}: probe() needs a probe_sample in the manifest")
        produces = manifest.get("produces") or []
        if not produces:
            raise ProviderError(f"{manifest['id']}: probe() needs a schema to produce")
        body = (self.prompt_root / sample).read_text(encoding="utf-8")
        return timed_probe(self, manifest, Payload(data=body, trust="T1"), produces[0], runs=3)

    # -- build-time checks -------------------------------------------------

    def _instruction(self, manifest: dict[str, Any]) -> Instruction:
        """Read the prompt envelope and hold it to the manifest and the schema."""
        name = manifest.get("system_prompt")
        if not name:
            raise ProviderError(f"{manifest['id']}: a gguf provider must name a system_prompt")
        instruction = Instruction.from_file(self.prompt_root / name)
        declared = manifest.get("system_prompt_sha256")
        if declared and instruction.digest() != declared:
            raise ProviderError(
                f"{name} is not the prompt this manifest pins.\n"
                f"  manifest: {declared}\n  on disk:  {instruction.digest()}"
            )
        for schema_id in manifest.get("produces") or ():
            schema = self.schemas.get(schema_id)
            if schema is not None and (problems := instruction.audit(schema)):
                raise ProviderError(f"{name} does not fit {schema_id}: " + "; ".join(problems))
        return instruction


__all__ = ["GgufAdapter", "file_digest"]
