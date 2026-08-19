"""Memory layers, sections 9.3 and 9.7 of the design.

Two stores, one SQLite file, one ingest pass:

* `EpisodicIndex` — cross-session memory. What happened last time.
* `FailureMemory` — what went wrong, and what worked instead.

Both are DERIVED from the session ledgers. Delete the database and rebuild it.
"""

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
