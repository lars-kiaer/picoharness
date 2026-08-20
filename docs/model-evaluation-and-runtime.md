# Model evaluation and runtime selection

**picoharness — companion note to `docs/serial-micro-agent-harness.md`**

Version 1.0
Language: ASD-STE100 Simplified Technical English

This note covers two decisions:

1. Which inference runtime and weight format to build against (section 1).
2. How to decide which models to use, and which specialist models to try
   (sections 2 to 6).

It is written to be handed to a person who will write code. Section 7 gives the
first task in full.

---

## 1. Runtime and weight format

### 1.1 Separate the three things

The usual list of options mixes three different layers.

| Layer | Options |
|-------|---------|
| Weight format | GGUF, safetensors, ONNX |
| Runtime | llama.cpp, PyTorch, ONNX Runtime, ExecuTorch, TensorFlow |
| Not applicable | Diffusers |

**Diffusers** is a library for diffusion models. It makes images, video, and
audio. It has no role in a text pipeline.

**TensorFlow** can be removed. Small language models are no longer published
for it.

### 1.2 The decision: GGUF on llama.cpp

Build the first model adapter against GGUF and llama.cpp. The reason is not
popularity. Five specific parts of the design depend on it.

| Design section | What it needs | Why llama.cpp |
|----------------|---------------|---------------|
| 7.2, 7.3 | `mmap` and page-cache residency | Built in. PyTorch reads weights into process memory and gives no page-cache story. |
| 10.2 level 1 | The model must be unable to emit invalid JSON | GBNF grammars are native. PyTorch needs `outlines` or `xgrammar`, which is one more dependency to provision offline. |
| 7.4 | A KV cache saved to disk and reloaded | llama.cpp has state save and load. PyTorch does not, without custom work. |
| P9, 14 | One static binary, no interpreter to provision | A C++ build cross-compiles to an ARM board. It is also the path to a Rust core without changing the inference layer. |
| 6.2 | Task-specific small models must be available | Liquid publishes GGUF for every Nano, with guides for llama.cpp among others. |

### 1.3 Where PyTorch still belongs

Three places, all **outside** the run-time path:

- **Fine-tuning**, later, with PEFT or Unsloth. Train in PyTorch, convert to
  GGUF, serve the GGUF.
- **A reference measurement.** Run a fixture set in fp16 to learn what the Q4
  quantization costs in accuracy. Do this once per model, not per release.
- **Models with no GGUF conversion.** Some vision-language models still have
  none.

### 1.4 ONNX Runtime is the second adapter, not the first

Use ONNX for:

- Profile C, the browser. See section 13 of the design document.
- Embedding models, and non-LLM specialists such as OCR or a classifier, where
  the ONNX path is often simpler than the GGUF path.

Note that the decision is already deferred by the architecture. The adapter
interface in section 7.1 declares `code`, `gguf`, `onnx`, and `binary`. Only the
order is open: `code` first, `gguf` in v2, `onnx` when something needs it.

### 1.5 In-process, not over HTTP

For v2, bind `llama-cpp-python` in-process rather than driving `llama-server`
over HTTP.

The reason is section 7.1.1. Scope unwind needs real control over load and
unload. A subprocess does not give it, and a serial design that swaps providers
many times per task will leak through the gap.

The cost is that the binding must be compiled. That is routine on Linux and
awkward on Windows. Let the target platform decide, and not the development
machine.

### 1.6 An open question for the pin list

Section 10.3 pins the model file, the quantization, the sampler, and the seed.
That may not be enough for llama.cpp. Numerical results can depend on the
**thread count** and on the CPU instruction sets the build uses, because the
reduction order changes.

Do not assume either way. Measure it:

```
Run one fixture with n_threads = 1 and with n_threads = 4.
Compare the output byte for byte.
```

Ten minutes. If the outputs differ, `n_threads` and the build flags belong in
the manifest and in the composition hash of section 4.6. Better to know now
than after six months of logs.

**Measured 2026-08-20. The outputs do not differ.** Three calls of
`normal-01-syslog-disk-io` through LFM2-350M-Extract Q4_K_M, greedy, seed 0,
under `llama-cpp-python` 0.3.35 on x86-64: `n_threads=1` and `n_threads=4` gave
the same bytes, and a repeat at 4 gave them again. The repeat matters, because
two runs that differ only in thread count cannot separate a threading effect
from ordinary variation.

So `n_threads` stays out of the composition hash, and `composition.py` needs no
change. Read the result at its true width. It covers one model, one build, one
machine, and greedy decoding, where a small difference in the sum only shows if
it moves an argmax. A build with different instruction sets could answer
differently, and the machine profile of section 4.6 already carries that case:
the composition would match and the machine hash would not. Measure again on
the first ARM board.

---

## 2. The harness is the evaluation harness

Do not build a separate evaluation rig. The parts already exist.

| Need | Already built |
|------|---------------|
| Ground truth | Golden-file fixtures, section 10.4 |
| Quality score | `v_provider_health`, section 9.7 |
| Cost score | `v_provider_cost`, section 12.4 |
| Failure detail | The failure taxonomy, section 9.7 |

The decision metric is **pass rate at a stated p90 cost**. Both axes are
already measured, per provider and per schema.

Register each candidate model as a provider. Run the same fixture set. Read the
two views.

### 2.1 The failure taxonomy tells you how, not only whether

The first five failure kinds map to the levels of the validation ladder.

| Caught at | Meaning for a candidate model |
|-----------|-------------------------------|
| `grammar` | Unusable. It cannot hold a format. |
| `schema` | Weak, but visible. The runtime rejects it every time. |
| `range` | It produces plausible values that are out of bounds. |
| `crosscheck` | **The dangerous one.** It lies convincingly, and only a second, deterministic path finds it. |
| `semantic` | It needs a critic to catch, which is expensive. |

A model that fails at level 2 is merely bad. A model that fails at level 4 is a
liability. Rank on where the failures land, not only on how many there are.

---

## 3. Four properties that no benchmark measures

Public benchmarks measure conversation, reasoning, and knowledge. A reducer
needs none of those. Measure these instead.

### 3.1 Abstention

**The most important property, and the least tested.**

When a field is not present in the input, does the model emit `null`, or does
it invent a plausible value? Grammar-constrained decoding guarantees valid JSON
either way. Section 10.1 of the design document explains why this failure is
the most dangerous one in the system.

**Fixture rule:** about one third of the fixtures for each schema must have
missing fields, with `null` as the expected value.

If you do not test this, you will find it in production.

### 3.2 Degradation with input length

Measure accuracy at about 500, 4000, and 16000 tokens of input.

Small models do not degrade in a straight line. They hold and then fall away.
The point where that happens sets the chunk size for every tool that returns
long output, which is an architecture decision and not a tuning detail.

### 3.3 Resistance to injected instructions

Reducers read T1 data. See section 11 of the design document.

Put an instruction inside a log line, a PDF, or a web page in the fixture, and
check whether the structured output changes.

The architecture already limits the damage, because a reducer cannot emit a
tool call. So this measures **data corruption**, not takeover. It is still a
per-model property, and models differ a lot.

### 3.4 Tokenizer efficiency for the target language

Five minutes of work with a direct effect on cost.

Run the same Danish text through each candidate tokenizer and count the tokens.
Tokens per word varies widely between model families, and it multiplies the CPU
cost of every call for the life of the system.

### 3.5 The unglamorous checks

Do these before any of the above, because they are disqualifying:

- The licence, and whether it permits the intended use.
- A GGUF conversion exists, and which quantizations.
- The chat template embedded in the GGUF file is correct. Do not hand-write a
  template; call the chat-completion API and let the runtime read it.
- The context window, and the real cost of using it on a CPU.

---

## 4. Candidates by capability

Match these to the capability table in section 6.1 of the design document.

| Capability | Try |
|------------|-----|
| `extract@1` | LFM2-350M-Extract, LFM2-1.2B-Extract, Qwen3 0.6B, Granite 350M |
| `route@1` | LFM2-1.2B-Tool, FunctionGemma 270M, Granite nano, Needle 26M |
| `plan@1` | LFM2.5-1.2B-Thinking, Qwen3 1.7B, Granite 4.1 3B |
| `answer@1`, Danish | The Munin 1.0 family, Gemma, Qwen3 |
| `embed@1` | EmbeddingGemma 308M, multilingual-e5-small, LFM2-ColBERT-350M |
| `classify@1` | **Not a model.** fastText, or logistic regression over embeddings |
| `verify@1` | Deterministic code first. A model only for what code cannot check. |

### 4.1 Notes

**Liquid Nanos cover four of these directly.** They ship at 350M and 1.2B, all
with GGUF, across translation, extraction, RAG, tool calling, and mathematical
reasoning. That is the reason to start there: the size, the format, and the
task split already match the design.

**Danish output must be measured, not assumed.** The Munin 1.0 family, released
in June 2026, is post-trained on top of several open models and is the best
starting point. It is not evidence until it passes your fixtures.

**One interesting outlier for routing.** Needle, at 26M parameters, is reported
to beat FunctionGemma 270M, Qwen 600M, Granite 350M, and LFM2.5 350M at
single-shot function calling, using an encoder architecture without a
feed-forward network. It is also reported to be unable to plan multi-step
tasks, resolve ambiguous references, or generalise to unseen tool schemas.

That combination fits the role split exactly. It can serve `route@1`. It must
never serve `plan@1`. This is what the capability model in section 6 is for.

**`classify@1` and much of `extract@1` should not use a model at all.** This is
principle P3. A regular expression, a parser, or a small classifier is faster,
cheaper, and exactly repeatable. Register those as `code` providers and let the
selection policy prefer them.

---

## 5. Fine-tuning: not yet

The published result is consistent: light fine-tuning on 500 to 1000
domain-specific examples beats prompting alone, and it beats it by a wide
margin.

**Do not do it now.** Fine-tuning needs domain data from a real use case, and
that is the one thing this project does not have. See section 17 of the design
document.

What to do instead: build the fixture set.

A golden-file fixture **is** a training pair. It is an input with its expected
structured output. Two hundred fixtures written over the next months give you
an evaluation suite and a LoRA dataset at the same time, with no extra work.

Design the fixture files with that second use in mind:

- One input, one expected output, one schema version per file.
- Keep the raw input, not a cleaned copy.
- Record which capability and which schema it belongs to.
- Keep the poisoned fixtures in a separate directory, so they never enter a
  training set by accident.

---

## 6. What does not go stale

Model rankings change every quarter. The fixture set does not.

The fixture set is the durable asset of this project. It is the reason a model
swap is a manifest edit and not a rewrite, and it is the reason a claim about a
model can be checked instead of believed.

Spend the time there, not on reading comparisons.

---

## 7. The first task

One afternoon. It produces the first real number.

### 7.1 Scope

Capability: `extract@1`. One schema. One tool that returns text.

### 7.2 Write 30 fixtures

| Count | Kind | Expected result |
|-------|------|-----------------|
| 20 | Normal input, all fields present | The correct values |
| 7 | One or more fields absent | `null` for those fields |
| 3 | An instruction injected into the input | The correct values, and no change of behaviour |

Store them as pairs: `fixtures/extract/<name>.input` and
`<name>.expected.json`. Keep the poisoned three under
`fixtures/adversarial/`.

### 7.3 Register three providers

Write three manifests against the same capability, per section 6.3:

- `extract-350m-q4` — LFM2-350M-Extract
- `extract-1.2b-q4` — LFM2-1.2B-Extract
- One from another family, for example Qwen3 0.6B

Leave the `cost` and `resource` fields as `null`. The probe fills them.

### 7.4 Run and read

```
1. Run the benchmark of section 12.1 to fill the manifests.
2. Run all 30 fixtures against all three providers.
3. Read v_provider_health   -> pass rate per provider per schema
4. Read v_provider_cost     -> p50 and p90 per provider
5. Read v_unresolved        -> which fixtures nothing could handle
```

### 7.5 What the result must tell you

- Which provider gives the best pass rate at an acceptable p90.
- Whether any provider invents values for absent fields. **If one does, do not
  use it, whatever its pass rate is.**
- Whether an injected instruction changed any output.
- Whether the extra 850M parameters of the 1.2B model bought anything on this
  schema. Often they do not, and that is a useful finding.

### 7.6 Do this at the same time

The thread-determinism check of section 1.6. Ten minutes, and it decides
whether the reproducibility contract in section 10.3 is complete.

---

## 8. Order of work

| Step | Task | Gate |
|------|------|------|
| 1 | The three measurements of section 12.1 on the target box | The table is filled |
| 2 | The thread-determinism check, section 1.6 | You know what to pin |
| 3 | 30 fixtures for one schema, section 7.2 | They exist and are versioned |
| 4 | The `gguf` adapter, in-process | It loads, runs, and unloads cleanly |
| 5 | Three providers, one capability, section 7.3 | The two views have numbers |
| 6 | The swap test of section 17 | A provider change is a manifest edit |

Do not start at step 4. An adapter with nothing to measure against tells you
that the code runs, which is not the question.

---

## Appendix — What was ruled out, and why

| Option | Verdict |
|--------|---------|
| Diffusers | Not applicable. It serves diffusion models, not text models. |
| TensorFlow | Removed. Small language models are not published for it. |
| PyTorch as the run-time path | No `mmap` story, no native grammar, no KV state to disk, and an interpreter to provision offline. Keep it for training and for reference measurements. |
| `llama-server` over HTTP | Simpler to operate, but it gives no control over unload. Section 7.1.1 needs that control. |
| A separate evaluation framework | The harness already measures quality and cost. A second one would drift from what actually ran. |
| Fine-tuning now | It needs domain data from a use case that does not exist yet. Build the fixtures instead; they become the training set. |
