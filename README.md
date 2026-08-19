# picoharness

**picoharness** is a serial micro-agent harness for a CPU-only edge machine.
The state is a file. The models are pure functions over it. One provider runs
at a time, and a provider can be a model, a parser, or a shell script.

The design is in [`docs/serial-micro-agent-harness.md`](docs/serial-micro-agent-harness.md).

This release implements the **memory layers** (sections 9.3 and 9.7). It has **no runtime
dependencies**. Everything is SQLite with FTS5, which ships with Python.

| Layer | Question it answers | Class |
|-------|--------------------|-------|
| Episodic index | What happened last time? | `EpisodicIndex` |
| Failure memory | What went wrong, and what worked instead? | `FailureMemory` |

Both are **derived**. The session ledgers (`events.jsonl`) are the only source
of truth. Delete the database file and rebuild it. A test asserts that a
rebuild loses nothing.

---

## Install

```bash
pip install -e ".[dev]"
pytest                      # 46 tests, about 1 second
```

Python 3.11 or later. No network access is needed at run time.

---

## Quick start

```python
from picoharness.memory import EpisodicIndex, FailureMemory, RecallPolicy

ix = EpisodicIndex("memory/episodic.db")
ix.ingest_dir("sessions/")          # idempotent; re-run it as often as you like
fm = FailureMemory(ix.conn)         # same file, same connection

ix.field_range("disk_free_pct", below=15)     # 0 model calls
ix.similar_episodes("disk pressure host-a")   # 0 model calls
ix.recall("io timeout", policy=RecallPolicy(budget_class="interactive"))
fm.prompt_block(fm.avoidance(capability="extract@1"))
```

Two runnable examples are in `examples/`.

---

## What a ledger looks like

One JSON object per line. The index reads these event types and ignores the
rest.

```jsonl
{"seq":0,"t":"…","type":"composition","session_id":"j1","hash":"sha256:41d0"}
{"seq":1,"t":"…","type":"user_input","session_id":"j1","text":"why is the disk full"}
{"seq":2,"t":"…","type":"step_started","step":"s1","tool":"read_syslog","trust":"T1"}
{"seq":3,"t":"…","type":"validation_failed","step":"s1","provider":"extract-350m-q4",
 "schema":"log_summary@2","capability":"extract@1","error":"error_count missing"}
{"seq":4,"t":"…","type":"fact_added","step":"s1","provider":"extract-1.2b-q4",
 "schema":"log_summary@2","fact":{"error_count":7,"first_error":"disk I/O timeout"}}
{"seq":5,"t":"…","type":"answer_sent","outcome":"answered"}
```

`step_started` carries the trust level of the tool. Every fact from that step
inherits it. See "Trust" below.

---

## Episodic index

### Retrieval cascade

Try the cheap path first, and stop when it answers.

| Path | Method | Model calls | Status |
|------|--------|-------------|--------|
| 1 | Structured filter, and FTS5 with BM25 | 0 | Implemented |
| 2 | Vector search over the same rows | 1 embedding | `Embedder` protocol only |
| 3 | Reduction over the top *k* | 1 more | Not here; it is a normal step |

Path 2 is deliberately unimplemented. Measure that path 1 is not enough before
you add an index that must be kept current. Changing the embedding model
invalidates the whole index, and on a CPU that is hours of work.

The task budget controls the cascade. Under `interactive`, path 1 only.

### The typed projection

Every fact already passed a schema check, so the values are typed before they
are stored. `fact_field` puts each scalar in a column:

```sql
SELECT f.observed_at, ff.num
FROM fact_field ff JOIN fact f ON f.id = ff.fact_id
WHERE ff.key = 'disk_free_pct' AND ff.num < 15;
```

This is why "when was the disk below 15 % free" needs no model at all.

### Provenance

Every result carries the session, the ledger path, and the sequence number of
the event that produced it:

```python
hit.provenance.cite()   # '/sessions/job-0001/events.jsonl#3'
```

---

## Failure memory

Two consumers, one table.

### 1. The planner

Before it commits to a step, the planner asks what went wrong last time.

```python
fm.prompt_block(fm.avoidance(capability="extract@1"))
```

```
Known failures to avoid:
- schema at extract-350m-q4 x2; worked: escalated -> extract-1.2b-q4
- tool_error at read_pdf x1; no known remedy
```

A failure record points at its remedy. Without that, the table is a list of
complaints. With it, the table is a routing hint.

Failures group by **signature**. `normalise_detail()` replaces numbers with
`#`, quoted strings with `<q>`, and paths with `<path>`, so that

```
column 'user_id' not found in row 41
column 'host'    not found in row 9182
```

become one row with `seen = 2`, and not two separate incidents.

### 2. You

Four SQL views and eight named queries.

```bash
pico-failures list                                  # what is available
pico-failures report --db memory/episodic.db        # run them all
pico-failures report --name demotion_candidates --floor 0.8
pico-failures sql                                   # print the SQL and run it yourself
```

| View | Purpose |
|------|---------|
| `v_signature` | One row per failure shape, with recovery counts |
| `v_provider_health` | Pass rate per provider and schema |
| `v_unresolved` | Failures with no known remedy — the real bug list |
| `v_capability_gap` | Capabilities with no provider — the shopping list |

The database is plain SQLite with no extensions, so you can also read it from
the `sqlite3` shell, or from R:

```r
con <- DBI::dbConnect(RSQLite::SQLite(), "memory/episodic.db")
DBI::dbGetQuery(con, "SELECT * FROM v_provider_health ORDER BY pass_rate")
```

`v_provider_health` is a union over both tables on purpose. A provider that
**never** succeeded has no rows in `fact`, and that is exactly the provider you
want to see in the report.

---

## Trust

The rule:

> A fact carries the trust level of its source, for ever. Memory never upgrades
> trust.

A hostile line in a log file is `T1`. A reducer extracts it into a fact. Three
weeks later the planner asks memory about that host, and the fact comes back
looking like history. If memory returns it as ordinary knowledge, the injection
has crossed the trust boundary through the back door.

So there are two calls, not one:

```python
ix.recall(...)              # any caller: T0, T1 and T2
ix.recall_for_control(...)  # planner and router: T0 and T2 only
```

The second raises `TrustViolation` if an untrusted fact reaches it. A filter
that can only fail silently is a filter you will not notice when it breaks.

The same problem appears in failure memory, in a place that is easy to miss: an
error string can quote the text it could not parse, and that text came from a
tool. So `failure.detail` keeps its own `detail_trust`, and `Avoidance` has no
free-text field at all. The planner sees structure only. A test scans the
planner output for injected content.

---

## Design rules

1. **Derived, not authoritative.** Rebuild from the ledgers at any time.
2. **No side door.** A fact enters through the validation ladder, or not at all.
3. **Trust is inherited and permanent.**
4. **Path 1 first.** Add vectors when you have measured that you need them.
5. **No model in the read path.** Everything here is SQL and BM25.

---

## What this package does not do

- It does not write ledgers. The runtime does that.
- It does not embed anything. `Embedder` is a plug point.
- It does not consolidate memory with a model. A small model that merges
  conflicting facts loses provenance quietly, and provenance is what makes the
  rest work. Store a summary as a derived view with pointers back instead.

---

## Layout

```
src/picoharness/memory/
  episodic.py    # ingest, cascade path 1, typed projection, trust
  failure.py    # taxonomy, signatures, planner API, SQL views, CLI
tests/                 # 46 tests, no fixtures on disk
examples/              # two runnable demos
```

MIT.
