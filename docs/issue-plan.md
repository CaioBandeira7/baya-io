# GitHub Issue Plan

Suggested milestones and issues, ready to create on GitHub once the repository is published. Labels: `privacy`, `infra`, `audio`, `asr`, `retrieval`, `ui`, `research`, `docs`, `good first issue`.

---

## Milestone 0 — Pre-publication hygiene

**#1 Pin dependency versions** `infra`
Export the working environment (`pip freeze` from the original `.venv`) and pin the five direct dependencies in `requirements.txt`. Record the Python, CUDA and cuDNN versions.

**#2 Move demo notes into an example file** `good first issue`
Move `FALLBACK_NOTES` (fictitious legal demo case) into `examples/notes.example.md` and load it by default. Behaviour must stay the same.

**#3 Document the notes file format** `docs`
Explain the `##` heading + table convention used by `load_document_notes`, with a short example.

**#4 Choose a license** `docs`

## Milestone 1 — Prototype hardening

**#5 Split the monolith into modules** `infra`
`audio/`, `vad/`, `asr/`, `detection/`, `generation/`, `ui/`, plus a `config.py`. No behaviour change, verified by a manual smoke test.

**#6 Configuration file** `infra`
Model names, Ollama URL, VAD parameters, CUDA paths and notes path move into a YAML/TOML file with defaults.

**#7 CPU fallback for Faster-Whisper** `asr`
Detect CUDA availability; fall back to `device="cpu", compute_type="int8"`.

**#8 Optional microphone capture** `audio`
Allow loopback, microphone, or both; label the source of each transcript segment.

**#9 Remove dead code** `good first issue`
Unused imports (`warnings`, `textwrap`, `ttk`) and unused functions (`classify_question`, `format_spoken_answer`).

**#10 Unit tests for text heuristics** `good first issue`
Tests for `normalized`, `is_question`, `match_topic` and the Markdown notes parser, using synthetic data.

**#11 Structured latency logging** `research`
Write per-stage timings (VAD end → ASR → match/LLM → display) to a local CSV, with no transcript content by default.

## Milestone 2 — Document Intelligence

**#12 Document loaders (PDF, DOCX, Markdown)** `retrieval`
Extract text with page and section metadata into a common `Document` schema.

**#13 Chunking strategy** `retrieval`
Configurable chunk size and overlap, heading-aware splitting for Markdown and DOCX.

**#14 Embedding generation + local vector index** `retrieval`
Pluggable embedding back-end; persistent local index; incremental re-indexing.

**#15 Semantic retriever** `retrieval`
Top-k retrieval with score threshold; optional BM25 hybrid.

**#16 Grounded generation with citations** `retrieval`
Prompt constrained to retrieved passages; output includes source IDs; abstention when nothing passes the threshold.

**#17 Citation display in UI** `ui`
Show document name, page and section beneath each cue; click to open the source.

## Milestone 3 — Context-Aware Assistance

**#18 Domain-agnostic question detection** `research`
Replace interview-specific rules with a classifier or LLM-based detector; evaluate precision and recall.

**#19 Conversation context tracker** `retrieval`
Rolling window of recent turns for query rewriting (resolving "that clause", "the previous point").

**#20 Multilingual ASR (Portuguese)** `asr`
Switch from `base.en` to a multilingual model with language detection or a selector.

**#21 Latency optimization** `research`
Model warm-up, retrieval caching, streaming LLM output; measure the impact of each.

## Milestone 4 — Desktop Experience

**#22 UI redesign following the concept mockup** `ui`
**#23 Project-specific document libraries** `ui` `retrieval`
**#24 Windows packaging and installer** `infra`

## Milestone 5 — Research and Evaluation

**#25 Evaluation dataset** `research`
Public or synthetic documents with annotated question→passage pairs for the technical, legal and business domains.

**#26 Retrieval benchmark harness** `research`
Recall@k, MRR, nDCG across chunking and retrieval strategies.

**#27 Latency benchmark** `research`
p50/p95 per stage across hardware and model sizes; local vs. cloud.

**#28 Citation reliability and hallucination evaluation** `research`
Supported-claim rate and correct-abstention rate.

**#29 Reproducibility package** `research` `docs`
Fixed configs, scripts and raw results in `benchmarks/`.
