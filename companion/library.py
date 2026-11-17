"""Local document library: ingestion, chunking and BM25 retrieval.

Supported formats: PDF, DOCX, TXT and Markdown. Everything is stored under
``<data_dir>/library``; nothing leaves the machine.

Consistency rule: every change (add, activate, deactivate, delete) bumps a
generation counter. Search results are cached per generation, so a change
invalidates the cache, and listeners are notified so the live session can
drop context that cites a deleted document.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import threading
import time
import unicodedata
import uuid
import zipfile
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import config


class DocumentError(ValueError):
    """A document was rejected. The message is safe to show to the user."""


# --- Text processing -----------------------------------------------------------

_STOPWORDS = set(
    """a an the and or but of to in on at for with by from as is are was were be been being it its this that these
    those there here what which who whom whose when where why how do does did done have has had having can could would
    should will shall may might must not no yes i you he she we they me him her us them my your our their his
    about into over under than then so if also just very more most some any each other such only own same too
    o os as um uma uns umas de do da dos das em no na nos nas por para com sem que e ou mas se nao sim ao aos
    pelo pela pelos pelas este esta estes estas esse essa isso isto aquele aquela foi ser sao era como mais
    menos muito muita seu sua seus suas ele ela eles elas nos voce voces lhe""".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")


def _fold(text: str) -> str:
    """Lowercase and strip accents, so "Responsabilidade" matches "responsabilidade"."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def tokenize(text: str) -> list[str]:
    words = []
    for token in _TOKEN.findall(_fold(text)):
        words.extend(_COMPOUNDS.get(token, token).split())
    return [t for t in words if t not in _STOPWORDS and (len(t) > 1 or t.isdigit())]


_COMPOUNDS = {"laptime": "lap time", "laptimes": "lap times", "dataset": "data set", "datasets": "data sets"}


def chunk_tokens(location: str, text: str) -> list[str]:
    """Tokens of a passage plus its section heading, counted twice.

    Headings often carry the question or topic (e.g. "### 10. How did you prevent data leakage?"),
    so they are indexed with extra weight. Page and line labels carry no meaning and are skipped.
    """
    heading = location[2:] if location.startswith("§ ") else ""
    return tokenize(heading) * 2 + tokenize(text)


def _clean(text: str) -> str:
    text = text.replace("\x00", " ").replace("­", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _has_words(text: str, minimum: int = 3) -> bool:
    return len(re.findall(r"\w{2,}", text)) >= minimum


# --- Data model -------------------------------------------------------------------

@dataclass
class Document:
    id: str
    name: str
    ext: str
    size: int
    sha256: str
    added_at: float
    active: bool = True
    chunk_count: int = 0


@dataclass
class Chunk:
    id: str
    doc_id: str
    ordinal: int
    location: str
    text: str
    tokens: list[str] = field(default_factory=list, repr=False)


@dataclass(frozen=True)
class SearchResult:
    chunk_id: str
    doc_id: str
    doc_name: str
    location: str
    text: str
    score: float
    matched_terms: tuple[str, ...]


Listener = Callable[[str, str], None]  # (event, doc_id)


# --- Extraction ----------------------------------------------------------------------

def _decode_text(data: bytes) -> str:
    if b"\x00" in data[:4096]:
        raise DocumentError("This file looks binary, not plain text.")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentError("Could not decode this text file.")


def _extract_pdf(data: bytes) -> list[tuple[str, str]]:
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise DocumentError("This file is not a valid PDF.")
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:  # noqa: BLE001 - pypdf raises several types here
                ok = 0
            if not ok:
                raise DocumentError("This PDF is password-protected.")
        units = []
        for number, page in enumerate(reader.pages, start=1):
            units.append((f"p. {number}", page.extract_text() or ""))
        return units
    except DocumentError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise DocumentError(f"This PDF could not be read ({type(exc).__name__}).") from exc


def _extract_docx(data: bytes) -> list[tuple[str, str]]:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise DocumentError("This file is not a valid DOCX document.")
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - python-docx raises many types
        raise DocumentError("This DOCX document could not be read.") from exc
    units: list[tuple[str, str]] = []
    heading, buffer, paragraph_no, start_no = None, [], 0, 1

    def flush() -> None:
        if buffer:
            label = f"§ {heading}" if heading else f"¶ {start_no}–{paragraph_no}"
            units.append((label, "\n".join(buffer)))

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        paragraph_no += 1
        style = (paragraph.style.name if paragraph.style is not None else "") or ""
        if style.lower().startswith(("heading", "título", "titulo", "title")):
            flush()
            heading, buffer, start_no = text[:80], [], paragraph_no + 1
            continue
        if not buffer:
            start_no = paragraph_no
        buffer.append(text)
    flush()
    for number, table in enumerate(document.tables, start=1):
        rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        units.append((f"table {number}", "\n".join(r for r in rows if r.strip(" |"))))
    return units


def _extract_markdown(data: bytes) -> list[tuple[str, str]]:
    text = _decode_text(data)
    units, heading, buffer = [], None, []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            if buffer:
                units.append((f"§ {heading}" if heading else "intro", "\n".join(buffer)))
            heading, buffer = match.group(1).strip()[:80], []
        else:
            buffer.append(re.sub(r"[*_`>]+", "", line))
    if buffer:
        units.append((f"§ {heading}" if heading else "intro", "\n".join(buffer)))
    return units


def _extract_txt(data: bytes) -> list[tuple[str, str]]:
    lines = _decode_text(data).splitlines()
    units, block, start = [], [], 1
    for number, line in enumerate(lines, start=1):
        if line.strip():
            if not block:
                start = number
            block.append(line)
        elif block:
            units.append((f"lines {start}–{number - 1}", "\n".join(block)))
            block = []
    if block:
        units.append((f"lines {start}–{len(lines)}", "\n".join(block)))
    return _merge_small_units(units, config.CHUNK_CHARS)


def _merge_small_units(units: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    """Merge consecutive short text blocks (``lines a–b``) into fuller units."""
    merged: list[tuple[str, str]] = []
    for label, text in units:
        if merged and len(merged[-1][1]) + len(text) < limit:
            first = merged[-1][0].split("–")[0]
            last = label.split("–")[-1]
            merged[-1] = (f"{first}–{last}", merged[-1][1] + "\n\n" + text)
        else:
            merged.append((label, text))
    return merged


_EXTRACTORS = {".pdf": _extract_pdf, ".docx": _extract_docx, ".md": _extract_markdown, ".txt": _extract_txt}


def split_into_chunks(text: str, limit: int, overlap: int) -> list[str]:
    """Split text into chunks of roughly ``limit`` characters on sentence boundaries."""
    text = _clean(text)
    if len(text) <= limit:
        return [text] if text else []
    sentences = re.split(r"(?<=[.!?;:])\s+|\n{2,}", text)
    pieces: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        while len(sentence) > limit:  # very long sentence: hard split on whitespace
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(sentence[:cut])
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    chunks, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) + 1 > limit:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            current = (tail + " " + piece).strip()
        else:
            current = (current + " " + piece).strip()
    if current:
        chunks.append(current)
    return chunks


def safe_name(filename: str) -> str:
    name = filename.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*').strip(" .")
    return name[:120] or "document"


# --- Library ---------------------------------------------------------------------------

class DocumentLibrary:
    K1 = 1.5
    B = 0.75
    CACHE_SIZE = 128

    def __init__(self, data_dir: Path | str | None = None,
                 chunk_chars: int = config.CHUNK_CHARS, overlap: int = config.CHUNK_OVERLAP_CHARS,
                 max_bytes: int = config.MAX_UPLOAD_MB * 1024 * 1024) -> None:
        self.root = Path(data_dir or config.DATA_DIR) / "library"
        self.files_dir = self.root / "files"
        self.index_path = self.root / "index.json"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.chunk_chars, self.overlap, self.max_bytes = chunk_chars, overlap, max_bytes
        self._lock = threading.RLock()
        self._docs: dict[str, Document] = {}
        self._chunks: dict[str, Chunk] = {}
        self._doc_chunks: dict[str, list[str]] = {}
        self._listeners: list[Listener] = []
        self._generation = 0
        self._stats_generation = -1
        self._df: Counter[str] = Counter()
        self._avgdl = 0.0
        self._n_active = 0
        self._cache: OrderedDict[tuple, list[SearchResult]] = OrderedDict()
        self._load()

    # -- listeners / state ----------------------------------------------------------
    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def _changed(self, event: str, doc_id: str) -> None:
        self._generation += 1
        self._cache.clear()
        self._save()
        for listener in list(self._listeners):
            listener(event, doc_id)

    @property
    def generation(self) -> int:
        return self._generation

    def list_documents(self) -> list[Document]:
        with self._lock:
            return sorted((Document(**asdict(d)) for d in self._docs.values()), key=lambda d: d.added_at)

    def get_document(self, doc_id: str) -> Document | None:
        with self._lock:
            doc = self._docs.get(doc_id)
            return Document(**asdict(doc)) if doc else None

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        with self._lock:
            return self._chunks.get(chunk_id)

    def is_chunk_live(self, chunk_id: str) -> bool:
        """True if the chunk exists and belongs to an active document."""
        with self._lock:
            chunk = self._chunks.get(chunk_id)
            return bool(chunk and self._docs.get(chunk.doc_id) and self._docs[chunk.doc_id].active)

    # -- mutations ----------------------------------------------------------------------
    def add(self, filename: str, data: bytes) -> tuple[Document, bool]:
        """Add a document. Returns (document, created); created is False for duplicates."""
        name = safe_name(filename)
        ext = Path(name).suffix.lower()
        if ext not in config.ALLOWED_EXTENSIONS:
            raise DocumentError("Unsupported file type. Use PDF, DOCX, TXT or Markdown.")
        if not data:
            raise DocumentError("The file is empty.")
        if len(data) > self.max_bytes:
            raise DocumentError(f"The file is larger than {self.max_bytes // (1024 * 1024)} MB.")
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            for doc in self._docs.values():
                if doc.sha256 == digest:
                    return Document(**asdict(doc)), False
        units = _EXTRACTORS[ext](data)
        doc_id = uuid.uuid4().hex[:12]
        chunks: list[Chunk] = []
        for location, text in units:
            for part in split_into_chunks(text, self.chunk_chars, self.overlap):
                if _has_words(part):
                    chunks.append(Chunk(f"{doc_id}-{len(chunks)}", doc_id, len(chunks), location, part,
                                        chunk_tokens(location, part)))
        if not chunks:
            hint = " It may be a scanned PDF without a text layer." if ext == ".pdf" else ""
            raise DocumentError("No readable text was found in this file." + hint)
        doc = Document(doc_id, name, ext, len(data), digest, time.time(), True, len(chunks))
        (self.files_dir / f"{doc_id}{ext}").write_bytes(data)
        with self._lock:
            self._docs[doc_id] = doc
            for chunk in chunks:
                self._chunks[chunk.id] = chunk
            self._doc_chunks[doc_id] = [c.id for c in chunks]
            self._changed("added", doc_id)
        return Document(**asdict(doc)), True

    def set_active(self, doc_id: str, active: bool) -> Document:
        with self._lock:
            doc = self._docs.get(doc_id)
            if doc is None:
                raise KeyError(doc_id)
            if doc.active != active:
                doc.active = active
                self._changed("activated" if active else "deactivated", doc_id)
            return Document(**asdict(doc))

    def delete(self, doc_id: str) -> None:
        with self._lock:
            doc = self._docs.pop(doc_id, None)
            if doc is None:
                raise KeyError(doc_id)
            for chunk_id in self._doc_chunks.pop(doc_id, []):
                self._chunks.pop(chunk_id, None)
            (self.files_dir / f"{doc_id}{doc.ext}").unlink(missing_ok=True)
            self._changed("deleted", doc_id)

    # -- search ---------------------------------------------------------------------------
    def _refresh_stats(self) -> None:
        if self._stats_generation == self._generation:
            return
        df: Counter[str] = Counter()
        total, count = 0, 0
        for chunk in self._chunks.values():
            if self._docs[chunk.doc_id].active:
                df.update(set(chunk.tokens))
                total += len(chunk.tokens)
                count += 1
        self._df, self._n_active = df, count
        self._avgdl = total / count if count else 0.0
        self._stats_generation = self._generation

    def search(self, query: str, k: int = config.TOP_K) -> list[SearchResult]:
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms:
            return []
        with self._lock:
            key = (self._generation, tuple(terms), k)
            if key in self._cache:
                self._cache.move_to_end(key)
                return list(self._cache[key])
            self._refresh_stats()
            scored = []
            for chunk in self._chunks.values():
                doc = self._docs[chunk.doc_id]
                if not doc.active:
                    continue
                tf = Counter(chunk.tokens)
                matched = tuple(t for t in terms if tf[t])
                if not matched:
                    continue
                score = 0.0
                for term in matched:
                    idf = math.log(1 + (self._n_active - self._df[term] + 0.5) / (self._df[term] + 0.5))
                    norm = 1 - self.B + self.B * len(chunk.tokens) / (self._avgdl or 1)
                    score += idf * tf[term] * (self.K1 + 1) / (tf[term] + self.K1 * norm)
                scored.append(SearchResult(chunk.id, doc.id, doc.name, chunk.location, chunk.text,
                                           round(score, 4), matched))
            scored.sort(key=lambda r: (r.score, len(r.matched_terms)), reverse=True)
            results = scored[:k]
            self._cache[key] = results
            if len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)
            return list(results)

    # -- persistence --------------------------------------------------------------------
    def _save(self) -> None:
        payload = {
            "version": 1,
            "documents": [asdict(d) for d in self._docs.values()],
            "chunks": [{"id": c.id, "doc_id": c.doc_id, "ordinal": c.ordinal, "location": c.location, "text": c.text}
                       for c in self._chunks.values()],
        }
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for raw in payload.get("documents", []):
            doc = Document(**raw)
            if (self.files_dir / f"{doc.id}{doc.ext}").exists():
                self._docs[doc.id] = doc
                self._doc_chunks[doc.id] = []
        for raw in payload.get("chunks", []):
            if raw["doc_id"] in self._docs:
                chunk = Chunk(raw["id"], raw["doc_id"], raw["ordinal"], raw["location"], raw["text"],
                              chunk_tokens(raw["location"], raw["text"]))
                self._chunks[chunk.id] = chunk
                self._doc_chunks[chunk.doc_id].append(chunk.id)

    def iter_live_chunk_ids(self) -> Iterable[str]:
        with self._lock:
            return [c for c in self._chunks if self.is_chunk_live(c)]
