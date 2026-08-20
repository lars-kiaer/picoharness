"""Event 0: what this run was made of. Section 4.6.

Section 10.3 pins each provider. That is not enough. Two runs can use the same
providers and still differ, because the composition differs: a different policy
file, a different tool version, a different schema registry.

So the runtime resolves every manifest, every policy and every schema version
into one plain document, hashes it, and writes the hash as the first event.

Two hashes, not one. The **composition** covers what was configured. The
**machine** covers what it was measured on. They are kept apart on purpose:

    A replay on the same machine must match both.
    A replay on different hardware matches the composition but not the machine.

That second case is exactly the warning you want. The plan is the same; the
route may not be. Section 6.4 filters providers on measured cost and remaining
budget, so a slow box selects a different provider from a fast one, and a
different provider gives a different answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _digest(document: Any) -> str:
    """One canonical rendering, one hash. Key order must never matter."""
    body = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# the machine
# --------------------------------------------------------------------------


def _total_ram_mb() -> int | None:
    """Physical memory, using only the standard library.

    Returns None rather than a guess. A wrong number here would enter the
    machine hash and make two unlike boxes look alike, which is the one thing
    this hash exists to prevent.
    """
    try:  # Linux, and most Unix
        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) // (1024 * 1024)
    except (AttributeError, ValueError, OSError):
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys) // (1024 * 1024)
        except Exception:
            # A probe must never stop a run. An unknown value is honest;
            # a guessed one would enter the machine hash and make two
            # unlike boxes look alike.
            return None
    return None


@dataclass(frozen=True, slots=True)
class MachineProfile:
    """One physical box: what it is, and what providers cost on it.

    `costs` is the part that changes behaviour. It holds the measured cost and
    the residency class of each provider, which is what the selection policy of
    section 6.4 filters on.
    """

    label: str
    system: str
    machine: str
    cores: int | None
    ram_mb: int | None
    costs: dict[str, dict[str, Any]] = field(default_factory=dict)
    representative: bool = True

    @classmethod
    def detect(cls, label: str, *, representative: bool = True, **costs: Any) -> MachineProfile:
        """Read the box this is running on.

        Set `representative=False` for a development machine. A number measured
        under WSL2 on a desktop is not an edge number, and section 12.4 is clear
        that a manifest written on one machine is a guess on the next. Recording
        that honestly is cheaper than remembering it.
        """
        return cls(
            label=label,
            system=platform.system(),
            machine=platform.machine(),
            cores=os.cpu_count(),
            ram_mb=_total_ram_mb(),
            costs=dict(costs),
            representative=representative,
        )

    def resolve(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "system": self.system,
            "machine": self.machine,
            "cores": self.cores,
            "ram_mb": self.ram_mb,
            "representative": self.representative,
            "costs": self.costs,
        }

    def digest(self) -> str:
        return _digest(self.resolve())


# --------------------------------------------------------------------------
# the composition
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Composition:
    """Everything that was configured, resolved into one plain document."""

    manifests: dict[str, Any] = field(default_factory=dict)
    schemas: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)

    def resolve(self) -> dict[str, Any]:
        """The document that gets hashed. It must be readable on demand.

        If you cannot print it, you cannot audit it. That is a requirement in
        4.6 and not a convenience.
        """
        return {
            "manifests": self.manifests,
            "schemas": self.schemas,
            "tools": self.tools,
            "policy": self.policy,
            "runtime": {
                "python": platform.python_version(),
                "picoharness": _version(),
                **self.runtime,
            },
        }

    def digest(self) -> str:
        return _digest(self.resolve())

    def dump(self, path: str | Path) -> Path:
        """Write the resolved document beside the ledger."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.resolve(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return target


def _version() -> str:
    from . import __version__

    return __version__


class CompositionMismatch(RuntimeError):
    """A replay was asked to run against a different configuration.

    Refusing is the default. Section 4.6 allows you to ask for the difference on
    purpose, and only on purpose.
    """


def boot(
    ledger: Any,
    composition: Composition,
    machine: MachineProfile,
    *,
    snapshot_dir: str | Path | None = None,
) -> tuple[str, str]:
    """Write event 0 and return the two hashes.

    Call this before anything else in a session. The ledger refuses every other
    event until it has one, so a run that cannot say what it was made of cannot
    start.
    """
    composition_hash = composition.digest()
    machine_hash = machine.digest()
    config_ref = None
    if snapshot_dir is not None:
        short = composition_hash.split(":")[1][:8]
        config_ref = str(composition.dump(Path(snapshot_dir) / f"boot-{short}.json"))

    ledger.append(
        "composition",
        hash=composition_hash,
        machine=machine_hash,
        config=config_ref,
    )
    return composition_hash, machine_hash


def check_replay(
    original: dict[str, Any],
    composition: Composition,
    machine: MachineProfile,
    *,
    allow_machine_change: bool = False,
) -> None:
    """Refuse a replay whose configuration has moved. Section 4.6.

    A machine change is reported separately, because it is a different and
    weaker statement: the plan is the same, but the route through it may not be.
    """
    if original.get("type") != "composition":
        raise CompositionMismatch(
            f"the original ledger starts with {original.get('type')!r}, not `composition`"
        )
    if original.get("hash") != composition.digest():
        raise CompositionMismatch(
            "the composition has changed since this ledger was written.\n"
            f"  recorded: {original.get('hash')}\n"
            f"  now:      {composition.digest()}\n"
            "A replay against a different configuration proves nothing."
        )
    if original.get("machine") != machine.digest() and not allow_machine_change:
        raise CompositionMismatch(
            "the same composition, on different hardware.\n"
            f"  recorded: {original.get('machine')}\n"
            f"  now:      {machine.digest()}\n"
            "The plan is the same; the route may not be, because the selection "
            "policy filters on measured cost. Pass allow_machine_change=True to "
            "run anyway."
        )


__all__ = [
    "Composition",
    "MachineProfile",
    "CompositionMismatch",
    "boot",
    "check_replay",
]
