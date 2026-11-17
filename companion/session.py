"""Live session state and the event bus that feeds the iPad over SSE.

Capture never starts on its own: ``start()`` must be called explicitly, and
``stop()`` clears the in-memory transcript and context. Nothing is written to
disk here.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import queue
import threading
import time
from typing import Any, Callable

from . import config
from .library import DocumentLibrary

PipelineFactory = Callable[[str, Callable[[str, str, float], None], Callable[[str], None]], Any]


class EventBus:
    """Fan-out of JSON events from worker threads to asyncio subscribers."""

    def __init__(self, max_queue: int = 500) -> None:
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self._max_queue)
        with self._lock:
            self._subscribers.add((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = {(l, s) for l, s in self._subscribers if s is not q}

    def publish(self, event: str, data: dict) -> None:
        message = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, q in subscribers:
            def put(q=q) -> None:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(message)
            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:  # loop closed: subscriber is gone
                self.unsubscribe(q)


class Session:
    MAX_TRANSCRIPT = 300
    MAX_CONTEXT = 50

    def __init__(self, library: DocumentLibrary, bus: EventBus,
                 pipeline_factory: PipelineFactory | None, assistant: Any | None) -> None:
        self.library, self.bus = library, bus
        self.pipeline_factory, self.assistant = pipeline_factory, assistant
        self.state = "idle"
        self.status = "Ready. Capture is off."
        self.source = config.DEFAULT_AUDIO_SOURCE
        self.started_at: float | None = None
        self.transcript: list[dict] = []
        self.context: list[dict] = []
        self._pipeline = None
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._turns: queue.Queue = queue.Queue(maxsize=2)
        self._worker = threading.Thread(target=self._assistant_loop, daemon=True)
        self._worker.start()
        library.add_listener(self._on_library_change)

    # -- state ---------------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "status": self.status, "source": self.source,
                    "started_at": self.started_at, "transcript": list(self.transcript[-100:]),
                    "context": list(self.context)}

    def _set_state(self, state: str, status: str) -> None:
        with self._lock:
            self.state, self.status = state, status
        self.bus.publish("state", {"state": state, "status": status, "source": self.source,
                                   "started_at": self.started_at})

    def set_status(self, status: str) -> None:
        with self._lock:
            self.status = status
        self.bus.publish("status", {"status": status})

    # -- control -------------------------------------------------------------------------
    def start(self, source: str) -> None:
        if source not in config.AUDIO_SOURCES:
            raise ValueError("Unknown audio source.")
        with self._lock:
            if self.state in ("listening", "starting"):
                return
            if self.pipeline_factory is None:
                raise RuntimeError("Audio pipeline is not available on this machine.")
            self.source, self.started_at = source, time.time()
            self.transcript, self.context = [], []
            self.state = "starting"
        self._set_state("starting", "Starting capture…")
        try:
            self._pipeline = self.pipeline_factory(source, self.add_turn, self.set_status)
            self._pipeline.start()
        except Exception as exc:  # noqa: BLE001 - report any start-up failure to the UI
            self._pipeline = None
            with self._lock:
                self.started_at = None
            self._set_state("idle", f"Could not start capture: {exc}")
            raise
        self._set_state("listening", "Listening")

    def stop(self) -> None:
        pipeline = self._pipeline
        self._pipeline = None
        if pipeline is not None:
            pipeline.stop()
        self._drain_turns()
        with self._lock:
            self.transcript, self.context, self.started_at = [], [], None
        self._set_state("idle", "Stopped. Capture is off and the session was cleared.")
        self.bus.publish("cleared", {})

    # -- turns and context ----------------------------------------------------------------
    def add_turn(self, speaker: str, text: str, ended_at: float) -> None:
        with self._lock:
            if self.state != "listening":
                return
            turn = {"id": next(self._ids), "speaker": speaker, "text": text, "time": time.time()}
            self.transcript.append(turn)
            del self.transcript[:-self.MAX_TRANSCRIPT]
        self.bus.publish("transcript", turn)
        if self.assistant is not None:
            try:
                self._turns.put_nowait((turn, ended_at))
            except queue.Full:  # keep latency low: drop the oldest pending turn
                try:
                    self._turns.get_nowait()
                except queue.Empty:
                    pass
                self._turns.put_nowait((turn, ended_at))

    def _drain_turns(self) -> None:
        while True:
            try:
                self._turns.get_nowait()
            except queue.Empty:
                return

    def _assistant_loop(self) -> None:
        while True:
            turn, ended_at = self._turns.get()
            if self.state != "listening":
                continue
            generation = self.library.generation
            item_id = next(self._ids)

            def partial(text: str, item_id=item_id, turn=turn) -> None:
                self.bus.publish("context_partial", {"id": item_id, "turn_id": turn["id"], "summary": text})

            def preview(item: dict, item_id=item_id, turn=turn) -> None:
                self.bus.publish("context", {**item, "id": item_id, "turn_id": turn["id"]})

            try:
                item = self.assistant.respond(turn["text"], partial, preview)
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                self.set_status(f"Assistant error: {exc}")
                continue
            if item is None:
                self.bus.publish("context_discard", {"id": item_id})
                continue
            item.update({"id": item_id, "turn_id": turn["id"], "pending": False,
                         "latency": round(time.perf_counter() - ended_at, 2)})
            with self._lock:
                recent = [{c["chunk_id"] for c in old["citations"]} for old in self.context[-3:]]
                evidence = {c["chunk_id"] for c in item["citations"]}
                if evidence and evidence in recent:
                    self.bus.publish("context_discard", {"id": item_id})  # same evidence as a recent card
                    continue
                # A document may have been deleted or deactivated while generating.
                if self.library.generation != generation and item["citations"]:
                    item["citations"] = [c for c in item["citations"] if self.library.is_chunk_live(c["chunk_id"])]
                    if not item["citations"]:
                        self.bus.publish("context_discard", {"id": item_id})
                        continue
                if self.state != "listening":
                    self.bus.publish("context_discard", {"id": item_id})
                    continue
                self.context.append(item)
                del self.context[:-self.MAX_CONTEXT]
            self.bus.publish("context", item)

    def _on_library_change(self, event: str, doc_id: str) -> None:
        if event in ("deleted", "deactivated"):
            self.purge_document(doc_id)
        self.bus.publish("documents", {"event": event, "doc_id": doc_id})

    def purge_document(self, doc_id: str) -> list[int]:
        """Drop every context item that cites ``doc_id``."""
        with self._lock:
            removed = [c["id"] for c in self.context if any(x["doc_id"] == doc_id for x in c["citations"])]
            self.context = [c for c in self.context if c["id"] not in removed]
        if removed:
            self.bus.publish("context_removed", {"ids": removed, "doc_id": doc_id})
        return removed
