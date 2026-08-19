"""picoharness — a serial micro-agent harness for CPU-only edge hardware.

The state is a file. The models are pure functions over it. One provider runs
at a time, and a provider can be a model, a parser, or a shell script.

See `docs/serial-micro-agent-harness.md` for the design.

Implemented so far: the memory layers. See `picoharness.memory`.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
