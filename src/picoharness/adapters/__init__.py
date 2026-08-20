"""Adapters: one per kind of provider.

Four kinds cover most needs — `code`, `gguf`, `onnx`, `binary`. The `code`
adapter comes first, on purpose. It has no dependencies, and it proves the
abstraction holds before a model is involved.
"""

from .base import Adapter, Handle, ProviderError, Reduced, Scope
from .code import CodeAdapter

__all__ = ["Adapter", "Handle", "Reduced", "Scope", "ProviderError", "CodeAdapter"]
