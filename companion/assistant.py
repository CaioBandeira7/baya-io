"""Turns a transcribed utterance into source-grounded context.

Flow: focus on the part of the turn that asks something -> BM25 search over the
active documents -> optional short summary from the local LLM that must cite
the numbered sources -> verification of citations and numbers. The original
excerpts are always returned so the user can check the summary.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable

import requests

from . import config
from .library import DocumentLibrary, SearchResult, tokenize

NO_ANSWER = "NO_ANSWER"
Generator = Callable[[str, Callable[[str], None]], str]

_QUESTION_START = ("what", "why", "how", "when", "where", "who", "which", "can you", "could you", "would you",
                   "do you", "did you", "have you", "are you", "is there", "is it", "tell me", "walk me",
                   "describe", "explain", "show me", "give me")


def is_request(text: str) -> bool:
    """True for questions and requests ("tell me...", "explain..."), not for plain statements."""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        lowered = re.sub(r"^((so|and|okay|ok|great|now|alright|well)[, ]+)+", "", sentence.lower().strip())
        if sentence.strip().endswith("?") or lowered.startswith(_QUESTION_START):
            return True
    return False


def focus_of(turn: str) -> str:
    """Return the part of a turn worth searching: from the last question/request to the end."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", turn) if s.strip()]
    if not sentences:
        return ""
    for i in range(len(sentences) - 1, -1, -1):
        lowered = re.sub(r"^((so|and|okay|ok|great|now|alright|well)[, ]+)+", "", sentences[i].lower())
        if sentences[i].endswith("?") or lowered.startswith(_QUESTION_START):
            return " ".join(sentences[i:])
    return " ".join(sentences[-2:])


def build_prompt(focus: str, turn: str, sources: list[dict]) -> str:
    listing = "\n\n".join(f"[{s['label']}] {s['doc_name']} ({s['location']}):\n{s['excerpt']}" for s in sources)
    return f"""You are Baya.io, a meeting companion that shows the user relevant facts from their own documents.

Using ONLY the SOURCES below, write 1 to 3 short sentences with the information most relevant to what was just said.
End every sentence with the source label it comes from, for example [S1].
Do not use outside knowledge. Do not invent numbers, names or dates.
If the sources do not contain relevant information, reply exactly: {NO_ANSWER}

SOURCES:
{listing}

WHAT WAS JUST SAID: {turn[-600:]}
FOCUS: {focus}

ANSWER:"""


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*%?")


def verify(summary: str, sources: list[dict]) -> tuple[str, list[str], set[str]]:
    """Remove invalid citation labels and report problems. Returns (summary, flags, cited labels)."""
    labels = {s["label"] for s in sources}
    flags: list[str] = []
    cited: set[str] = set()

    def check(match: re.Match) -> str:
        label = match.group(1).upper()
        if label in labels:
            cited.add(label)
            return f"[{label}]"
        flags.append(f"Removed a citation to a source that does not exist ({label}).")
        return ""

    summary = re.sub(r"\[(S\d+)\]", check, summary, flags=re.I)
    summary = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"[ \t]{2,}", " ", summary)).strip()
    if summary and not cited:
        flags.append("The summary did not cite any source. Rely on the excerpts below.")
    evidence = " ".join(s["excerpt"] for s in sources)
    for number in set(_NUMBER.findall(re.sub(r"\[S\d+\]", "", summary))):
        if number.rstrip("%") not in evidence:
            flags.append(f"The number {number} does not appear in the cited excerpts.")
    return summary, flags, cited


class OllamaGenerator:
    """Streams a completion from the local Ollama server."""

    def __init__(self, url: str = config.OLLAMA_URL, model: str = config.OLLAMA_MODEL,
                 max_tokens: int = config.LLM_MAX_TOKENS) -> None:
        self.url, self.model, self.max_tokens = url, model, max_tokens
        self.session = requests.Session()
        self._down_until = 0.0  # after a failure, skip the model briefly instead of waiting on every turn

    def warm_up(self) -> None:
        self.session.post(self.url, json={"model": self.model, "prompt": "Hi", "stream": False,
                                          "keep_alive": "30m", "options": {"num_predict": 1}}, timeout=(5, 180))

    def __call__(self, prompt: str, on_text: Callable[[str], None]) -> str:
        if time.monotonic() < self._down_until:
            raise requests.ConnectionError("Ollama was unreachable a moment ago")
        try:
            return self._generate(prompt, on_text)
        except requests.RequestException:
            self._down_until = time.monotonic() + 20
            raise

    def _generate(self, prompt: str, on_text: Callable[[str], None]) -> str:
        parts: list[str] = []
        with self.session.post(self.url, json={
            "model": self.model, "prompt": prompt, "stream": True, "keep_alive": "30m",
            "options": {"temperature": 0.1, "num_predict": self.max_tokens, "num_ctx": 4096},
        }, stream=True, timeout=(5, 120)) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if data.get("response"):
                    parts.append(data["response"])
                    on_text("".join(parts).strip())
                if data.get("done"):
                    break
        return "".join(parts).strip()


class Assistant:
    def __init__(self, library: DocumentLibrary, generator: Generator | None = None, k: int = config.TOP_K,
                 min_terms: int = config.MIN_MATCHED_TERMS, min_score: float = config.MIN_BM25_SCORE) -> None:
        self.library, self.generator = library, generator
        self.k, self.min_terms, self.min_score = k, min_terms, min_score

    def relevant(self, query: str) -> list[SearchResult]:
        terms = set(tokenize(query))
        # Short questions ("What's your name?") have one or two content words: require all of them.
        needed = min(self.min_terms, len(terms)) if len(terms) <= 2 else self.min_terms
        short = 0 < len(terms) <= 2
        # BM25 scores are low in small libraries, so a short question that matches every
        # one of its words is accepted without the score threshold.
        return [r for r in self.library.search(query, self.k)
                if len(r.matched_terms) >= max(1, needed)
                and (r.score >= self.min_score or (short and set(r.matched_terms) == terms))]

    def respond(self, turn: str, on_partial: Callable[[str], None] = lambda _: None,
                on_sources: Callable[[dict], None] | None = None) -> dict | None:
        focus = focus_of(turn)
        results = self.relevant(focus) or (self.relevant(turn) if focus != turn else [])
        request = is_request(turn)
        if not request:
            # Plain statements ("Good question, let me check...") need stronger evidence.
            results = [r for r in results if len(r.matched_terms) >= self.min_terms + 2]
        if not results:
            # A question with nothing in the documents gets a small notice instead of silence.
            return {"query": focus, "summary": "", "citations": [], "flags": [], "no_match": True,
                    "model": None} if request else None
        sources = [{"label": f"S{i}", "chunk_id": r.chunk_id, "doc_id": r.doc_id, "doc_name": r.doc_name,
                    "location": r.location, "excerpt": r.text, "matched_terms": list(r.matched_terms),
                    "score": r.score, "cited": False}
                   for i, r in enumerate(results, start=1)]
        summary, flags = "", []
        if on_sources is not None:  # lets the UI show the excerpts while the summary is generated
            on_sources({"query": focus, "summary": "", "citations": sources, "flags": [],
                        "pending": self.generator is not None})
        if self.generator is not None:
            try:
                raw = self.generator(build_prompt(focus, turn, sources), on_partial)
            except (requests.RequestException, ValueError) as exc:
                raw, flags = "", [f"Local model unavailable ({type(exc).__name__}). Showing excerpts only."]
            if raw and NO_ANSWER not in raw:
                summary, more_flags, cited = verify(raw, sources)
                flags += more_flags
                for source in sources:
                    source["cited"] = source["label"] in cited
        # Show one answer, not a list: the cited source(s), or the single best match.
        shown = [s for s in sources if s["cited"]] or sources[:1]
        return {"query": focus, "summary": summary, "citations": shown, "flags": flags,
                "no_match": False, "model": getattr(self.generator, "model", None)}
