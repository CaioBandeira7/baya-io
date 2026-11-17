# Roadmap and Research Plan

> Status: 🚧 Work in Progress. Nothing in Phases 2–5 has been implemented or evaluated yet. This document states intentions, not results.

**Research question**

> *How can real-time retrieval-augmented generation provide accurate, source-grounded information during live conversations while minimizing response latency?*

Legend: ✅ implemented · 🟡 partial / precursor · ⬜ planned

---

## Phase 0 — Repository hygiene (prerequisite)

- ✅ Personal content replaced by a fictitious demo case; neutral window titles and labels
- ⬜ Move the demo notes into an example notes file
- ⬜ Pin dependency versions from the working environment
- ⬜ Choose a license

## Phase 1 — Initial Prototype

| Item | Status | Evidence in code |
|---|---|---|
| Local audio capture | ✅ | `capture_frames`: WASAPI loopback via `soundcard` |
| Speech recognition | ✅ | `transcribe_audio`: Faster-Whisper `base.en`, CUDA |
| Local LLM integration | ✅ | `generate_answers`: Ollama `llama3.2:3b` |
| Basic desktop interface | ✅ | Tkinter always-on-top overlay |

Remaining work: modularize the single file, add a CPU fallback, make paths and models configurable, and add microphone capture as an option.

## Phase 2 — Document Intelligence

| Item | Status | Notes |
|---|---|---|
| Document ingestion | 🟡 | Only one Markdown notes file (table cells) today. Planned: PDF, DOCX, Markdown with page/section metadata |
| Semantic search | ⬜ | Embeddings + local vector index |
| RAG | ⬜ | Generator constrained to retrieved passages |
| Source citations | ⬜ | Each cue linked to document, page and section |

Open questions: chunk size and overlap for meeting-style queries; how domain (legal vs. technical) affects retrieval; whether to use hybrid retrieval (BM25 + dense).

## Phase 3 — Context-Aware Assistance

| Item | Status | Notes |
|---|---|---|
| Question detection | 🟡 | Rule-based, interview-specific (`is_question`) |
| Context tracking | ⬜ | Rolling window of recent turns used to rewrite queries |
| Evidence-grounded information | 🟡 | V8 shows notes-only cues with abstention; no document evidence yet |
| Retrieval optimization | ⬜ | Caching, pre-fetching, re-ranking under a latency budget |

## Phase 4 — Desktop Experience

| Item | Status | Notes |
|---|---|---|
| Improved interface | ⬜ | Direction shown in `assets/baya-concept.png` (mockup) |
| Document libraries | ⬜ | Per-project collections |
| Floating assistant | 🟡 | Always-on-top Tkinter window exists |
| Windows packaging | ⬜ | Installer handling CUDA/Ollama prerequisites |

## Phase 5 — Research and Evaluation

No measurements have been taken yet. Planned methodology:

| Dimension | Planned metric(s) | Planned approach |
|---|---|---|
| Retrieval accuracy | Recall@k, MRR, nDCG | Small annotated question→passage sets per domain, using public or synthetic documents |
| Citation reliability | Share of displayed statements supported by the cited passage | Manual annotation plus automated entailment checks |
| Hallucination | Unsupported-claim rate; correct-abstention rate | Questions with and without supporting evidence |
| End-to-end latency | p50 / p95 time from end of speech to display, per stage | Instrumentation of the ASR, retrieval and generation stages |
| Transcription | Word Error Rate | Recorded test prompts with consent, or public speech datasets |
| Local vs. cloud | Latency, quality, cost, privacy trade-offs | Same pipeline with interchangeable inference back-ends |
| Retrieval strategies | All of the above | Dense vs. BM25 vs. hybrid; chunking variants; re-ranking on/off |

Deliverables: a `benchmarks/` folder with scripts, fixed configurations and raw results, so every reported number can be reproduced.

## Privacy-aware processing (cross-cutting)

- Local-first inference. Any cloud option is explicit and opt-in.
- No storage of audio by default; transcripts stored only if the user enables it.
- Clear in-app reminders about consent.
