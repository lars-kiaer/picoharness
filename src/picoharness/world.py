"""One execution world, section 11.3.

Do not give each tool its own sandbox configuration. Make the file system and
the subprocess launcher one seam, and let every tool run through it.

    tool -> ctx.exec -> { fs provider, subprocess provider, limits }

The gain is that one swap moves everything. Point the seam at a container, at a
different machine, or at a read-only snapshot, and every tool follows. No tool
needs a variant, and no tool can opt out — which is the part that matters,
because a tool that can opt out eventually will.

`LocalWorld` is the v1 implementation: a read-only view of declared paths, no
subprocess, and a hard output cap. It is deliberately weak. It enforces what can
be enforced in-process and refuses what cannot, rather than pretending. The v7
implementation wraps `bubblewrap` or `systemd-run` behind the same interface, and
no tool changes.

Note what a file system sandbox does **not** do, because section 11.3 says to
state both: it does not stop a network call, and it does not hide other
processes. Those need the real thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .payload import Payload


class WorldError(RuntimeError):
    """A tool asked the world for something it is not allowed to have."""


@dataclass(frozen=True, slots=True)
class Limits:
    """What one tool run may consume. Section 11.3."""

    wall_ms: int = 5_000
    cpu_ms: int = 5_000
    max_output_bytes: int = 1 << 20
    network: bool = False

    def resolve(self) -> dict[str, Any]:
        return {
            "wall_ms": self.wall_ms,
            "cpu_ms": self.cpu_ms,
            "max_output_bytes": self.max_output_bytes,
            "network": self.network,
        }


class World(Protocol):
    """The one seam every tool goes through."""

    def read(self, path: str, *, trust: str = "T1") -> Payload: ...

    def run(self, argv: list[str], *, trust: str = "T1") -> Payload: ...


@dataclass(slots=True)
class LocalWorld:
    """In-process, read-only, no subprocess. The v1 world.

    `roots` is an allow-list. A tool that declares no path reads nothing, which
    is the right default: a tool gets access because someone wrote it down, not
    because the process happens to have it.
    """

    roots: tuple[Path, ...] = ()
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def rooted_at(cls, *paths: str | Path, **limits: Any) -> LocalWorld:
        return cls(
            roots=tuple(Path(p).resolve() for p in paths),
            limits=Limits(**limits) if limits else Limits(),
        )

    def _permitted(self, target: Path) -> Path:
        """Resolve and check. Resolving first is what closes `../` and symlinks."""
        resolved = Path(target).resolve()
        for root in self.roots:
            if resolved == root or root in resolved.parents:
                return resolved
        raise WorldError(
            f"{resolved} is outside the declared roots {[str(r) for r in self.roots]}"
        )

    def read(self, path: str, *, trust: str = "T1") -> Payload:
        """Read a file as bytes, cap it, and label it.

        Bytes rather than text, because the payload is blob-ready and a tool
        that returns a scanned page must not be forced through a decoder here.
        The trust level comes from the tool manifest, not from the content: what
        makes a log line untrusted is where it came from.
        """
        target = self._permitted(Path(path))
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise WorldError(f"cannot read {target}: {exc}") from exc

        payload = Payload(data=raw, mime="text/plain", trust=trust)  # type: ignore[arg-type]
        return payload.truncated(self.limits.max_output_bytes)

    def run(self, argv: list[str], *, trust: str = "T1") -> Payload:
        """Refused in v1, on purpose.

        An in-process world cannot impose a CPU limit, a wall-clock limit or a
        process view on a child. Running the command anyway would give a tool
        the freedom of the whole machine while the manifest claimed it was
        sandboxed, and a sandbox that is believed but absent is worse than none.
        """
        raise WorldError(
            f"LocalWorld runs no subprocess; {argv[0] if argv else '?'} needs the "
            f"sandboxed world of section 11.3"
        )

    def resolve(self) -> dict[str, Any]:
        """For the composition hash. A different world is a different system."""
        return {
            "kind": "local",
            "roots": [str(r) for r in self.roots],
            "limits": self.limits.resolve(),
        }


__all__ = ["World", "LocalWorld", "Limits", "WorldError"]
