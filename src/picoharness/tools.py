"""Built-in tools. Section 8.1.

A tool declares its own reducer, its effect, and the trust level of its output.
The dispatcher does not know how a log looks; it only knows the goal.

Everything a tool touches goes through the world of section 11.3, so a tool
cannot reach the file system directly and cannot opt out of the limits.
"""

from __future__ import annotations

from typing import Any

from .payload import Payload
from .runtime import Tool
from .world import World


def _read(world: World, args: dict[str, Any], trust: str) -> Payload:
    return world.read(args["path"], trust=trust)


def read_log(name: str = "read_log", schema: str = "log_summary@2") -> Tool:
    """Read a log file. Its output is T1: a log can hold what an attacker wrote."""
    return Tool(
        name=name,
        run=lambda world, args: _read(world, args, "T1"),
        reducer="extract@1",
        output_schema=schema,
        effect="read_only",
        idempotent=True,
        trust_out="T1",
    )


def read_disk(name: str = "read_disk", schema: str = "disk_usage@1") -> Tool:
    """Read a `df` capture. T2, because the system produced the numbers itself."""
    return Tool(
        name=name,
        run=lambda world, args: _read(world, args, "T2"),
        reducer="extract@1",
        output_schema=schema,
        effect="read_only",
        idempotent=True,
        trust_out="T2",
    )


__all__ = ["read_log", "read_disk"]
