"""Three questions the episodic index answers with no model call."""

import tempfile
from pathlib import Path

from picoharness.memory import EpisodicIndex, RecallPolicy
from picoharness.memory.samples import DISK_A, DISK_B, write_ledgers

root = write_ledgers(Path(tempfile.mkdtemp()) / "sessions",
                     {"job-0001": DISK_A, "job-0002": DISK_B})
ix = EpisodicIndex(root.parent / "memory" / "episodic.db")
ix.ingest_dir(root)
print("stats:", ix.stats(), "\n")

print("Q1  when was the disk below 15 % free")
for h in ix.field_range("disk_free_pct", below=15):
    print(f"    {h.payload}  trust={h.trust}  {h.provenance.cite()}")

print("\nQ2  have I looked at this before")
for r in ix.similar_episodes("disk pressure host-a"):
    print(f"    {r['session_id']}  {r['outcome']:9} fails={r['fail_count']}  {r['goal']}")

print("\nQ3  free text, and the same query for a control caller")
for h in ix.recall("io timeout", policy=RecallPolicy(budget_class="interactive")):
    print(f"    trust={h.trust}  {h.payload}")
print("    for plan@1:", ix.recall_for_control("io timeout") or "(nothing: the match is T1)")
ix.close()
