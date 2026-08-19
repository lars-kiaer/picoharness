"""Memory layers, sections 9.3 and 9.7 of the design.

Two stores, one SQLite file, one ingest pass:

* `EpisodicIndex` — cross-session memory. What happened last time.
* `FailureMemory` — what went wrong, and what worked instead.
* `CostModel` — what a provider actually costs on THIS machine.

Both are DERIVED from the session ledgers. Delete the database and rebuild it.
"""

from .cost import COST_SCHEMA, CostModel, Estimate
from .episodic import (
    EpisodicIndex,
    Provenance,
    Recalled,
    RecallPolicy,
    TrustViolation,
)
from .failure import (
    REPORT_SQL,
    Avoidance,
    FailureMemory,
    classify_event,
    normalise_detail,
    signature_for,
)

__all__ = [
    "EpisodicIndex",
    "CostModel",
    "Estimate",
    "COST_SCHEMA",
    "RecallPolicy",
    "Recalled",
    "Provenance",
    "TrustViolation",
    "FailureMemory",
    "Avoidance",
    "REPORT_SQL",
    "classify_event",
    "signature_for",
    "normalise_detail",
]
