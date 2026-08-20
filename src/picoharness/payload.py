"""What moves between a tool, a provider, and the ledger.

One type carries all of it: text, a scanned page, an audio file. Section 16
question 4 asks whether a second modality is in scope for v1. The answer taken
is "not yet, but the pipe is wide enough". Only text providers exist. The
signature already accepts bytes, because widening it later means changing every
adapter, every hook, and every test at once.

A payload also carries its trust level. That is not decoration: it is how the
level travels from the tool that produced the data to the fact derived from it,
without anyone having to remember to pass it along.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .trust import Trust, check

TEXT = "text/plain"
JSON = "application/json"


@dataclass(frozen=True, slots=True)
class Payload:
    """A unit of data with its type and its provenance.

    `data` is `str` for text and `bytes` for anything else. Keep the raw form.
    A provider that wants text from bytes decodes it itself, and records that it
    did, because the decoding can fail and that failure is information.
    """

    data: str | bytes
    mime: str = TEXT
    trust: Trust = "T1"
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check(self.trust)

    @property
    def is_text(self) -> bool:
        return isinstance(self.data, str)

    @property
    def nbytes(self) -> int:
        return len(self.data) if isinstance(self.data, bytes) else len(self.data.encode("utf-8"))

    def as_text(self, *, encoding: str = "utf-8") -> str:
        """Text form, or a clear error. Never a silent replacement.

        A mangled character becomes a wrong extracted value three steps later,
        with nothing in the ledger to explain it. Fail here instead.
        """
        if isinstance(self.data, str):
            return self.data
        try:
            return self.data.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ValueError(f"payload of {self.mime} is not {encoding} text") from exc

    def truncated(self, max_bytes: int) -> Payload:
        """Cut to a byte limit, and say so in `meta`.

        Section 5.3 sets `max_bytes_to_model` as a breaker. A cut that leaves no
        trace is a cut that will be blamed on the provider.
        """
        if self.nbytes <= max_bytes:
            return self
        if isinstance(self.data, bytes):
            cut: str | bytes = self.data[:max_bytes]
        else:
            cut = self.data.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        return Payload(
            data=cut,
            mime=self.mime,
            trust=self.trust,
            meta={**self.meta, "truncated_from": self.nbytes},
        )


def text(data: str, *, trust: Trust = "T1", **meta: Any) -> Payload:
    """The common case, spelled short."""
    return Payload(data=data, mime=TEXT, trust=trust, meta=meta)


__all__ = ["Payload", "text", "TEXT", "JSON"]
