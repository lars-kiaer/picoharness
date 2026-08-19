"""The two consumers of the failure memory, side by side."""

import tempfile
from pathlib import Path

from picoharness.memory import REPORT_SQL, EpisodicIndex, FailureMemory
from picoharness.memory.samples import BUILD_OK, BUILD_OK_2, DEAD_END, TAINTED, write_ledgers

root = write_ledgers(
    Path(tempfile.mkdtemp()) / "sessions",
    {"j1": BUILD_OK, "j2": BUILD_OK_2, "j3": DEAD_END, "j4": TAINTED},
)
ix = EpisodicIndex(root.parent / "memory" / "episodic.db")
ix.ingest_dir(root)
fm = FailureMemory(ix.conn)

print("=" * 68)
print("CONSUMER 1 - the planner, before it commits to extract@1")
print("=" * 68)
print(fm.prompt_block(fm.avoidance(capability="extract@1", limit=20)))
print("\nNo free text. The syslog error quoted an injected instruction, and")
print("the planner never reads a detail string.")

print("\n" + "=" * 68)
print("CONSUMER 2 - you, asking what is going wrong")
print("=" * 68)
for name in ("provider_health", "unresolved", "gaps", "recovery"):
    doc, _ = REPORT_SQL[name]
    print(f"\n-- {name}: {doc}")
    rows = fm.report(name, min_n=1)
    if not rows:
        print("   (none)")
        continue
    cols = rows[0].keys()
    print("   " + " | ".join(cols))
    for r in rows:
        print("   " + " | ".join(str(r[c]) for c in cols))
ix.close()
