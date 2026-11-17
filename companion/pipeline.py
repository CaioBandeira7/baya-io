"""Audio capture -> voice activity detection -> Faster-Whisper -> complete turns.

Signal-processing parameters match ``src/baya_v8.py``. That script builds its
Tkinter window at import time, so it cannot be imported; the logic lives here
as small classes (``VadSegmenter``, ``TurnAssembler``) that are testable without
audio hardware. Windows-only libraries are imported lazily.
"""

from __future__ import annotations

import collections
import os
import queue
import re
import threading
import time
from typing import Callable

import numpy as np

from . import config

TurnCallback = Callable[[str, str, float], None]  # (speaker, text, ended_at)
StatusCallback = Callable[[str], None]

_FILLERS = {"um", "uh", "okay", "ok", "yeah", "mm", "hmm", "right", "so", "you"}


def is_meaningful(text: str) -> bool:
    """Skip silence hallucinations and one-word fillers."""
    words = re.sub(r"[^a-z' ]", " ", text.lower()).split()
    if not words:
        return False
    if len(words) <= 2 and all(w in _FILLERS for w in words):
        return False
    return len(words) >= 2 or "?" in text


class VadSegmenter:
    """Groups 30 ms frames into speech segments (same rules as V8)."""

    def __init__(self, is_speech: Callable[[bytes, int], bool]) -> None:
        self.is_speech = is_speech
        self.pre_roll: collections.deque = collections.deque(maxlen=10)
        self.speaking = False
        self.frames: list[np.ndarray] = []
        self.voiced = self.silent = 0
        self.silence_limit = max(1, config.END_SILENCE_MS // config.FRAME_MS)
        self.max_frames = int(config.MAX_SPEECH_SECONDS * 1000 / config.FRAME_MS)

    def feed(self, frame: np.ndarray) -> np.ndarray | None:
        frame = np.clip(frame, -1, 1)
        rms = float(np.sqrt(np.mean(frame * frame)))
        pcm = (frame * 32767).astype(np.int16).tobytes()
        voiced = rms >= config.MIN_RMS and self.is_speech(pcm, config.RATE)
        if not self.speaking:
            self.pre_roll.append(frame)
            if voiced:
                self.speaking, self.frames = True, list(self.pre_roll)
                self.pre_roll.clear()
                self.voiced, self.silent = 1, 0
            return None
        self.frames.append(frame)
        if voiced:
            self.voiced, self.silent = self.voiced + 1, 0
        else:
            self.silent += 1
        if self.silent >= self.silence_limit or len(self.frames) >= self.max_frames:
            segment = None
            if self.voiced * config.FRAME_MS >= config.MIN_SPEECH_MS:
                segment = np.concatenate(self.frames).astype(np.float32)
            self.speaking, self.frames, self.voiced, self.silent = False, [], 0, 0
            self.pre_roll.clear()
            return segment
        return None


class TurnAssembler:
    """Merges consecutive segments from one speaker until they pause for ``gap`` seconds."""

    def __init__(self, gap: float = config.TURN_GAP_S) -> None:
        self.gap = gap
        self._parts: dict[str, list[str]] = collections.defaultdict(list)
        self._last_end: dict[str, float] = {}
        self._pending: collections.Counter = collections.Counter()
        self._speaking: set[str] = set()
        self._lock = threading.Lock()

    def segment_queued(self, speaker: str) -> None:
        with self._lock:
            self._pending[speaker] += 1

    def voice(self, speaker: str, active: bool) -> None:
        with self._lock:
            (self._speaking.add if active else self._speaking.discard)(speaker)

    def transcribed(self, speaker: str, text: str, ended_at: float) -> None:
        with self._lock:
            self._pending[speaker] = max(0, self._pending[speaker] - 1)
            if text:
                self._parts[speaker].append(text)
            self._last_end[speaker] = ended_at

    def ready(self, now: float) -> list[tuple[str, str, float]]:
        out = []
        with self._lock:
            for speaker, parts in self._parts.items():
                if (parts and not self._pending[speaker] and speaker not in self._speaking
                        and now - self._last_end[speaker] >= self.gap):
                    out.append((speaker, " ".join(parts).strip(), self._last_end[speaker]))
                    self._parts[speaker] = []
        return out


class WhisperService:
    """Loads Faster-Whisper once and keeps it for later sessions."""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()

    def model(self, on_status: StatusCallback = lambda _: None):
        with self._lock:
            if self._model is None:
                for path in config.CUDA_DLL_DIRS:
                    if os.path.isdir(path):
                        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
                        if hasattr(os, "add_dll_directory"):
                            os.add_dll_directory(path)
                from faster_whisper import WhisperModel

                on_status(f"Loading speech model ({config.WHISPER_MODEL} on {config.WHISPER_DEVICE})…")
                model = WhisperModel(config.WHISPER_MODEL, device=config.WHISPER_DEVICE,
                                     compute_type=config.WHISPER_COMPUTE, cpu_threads=4)
                list(model.transcribe(np.zeros(config.RATE, dtype=np.float32), language="en", beam_size=2)[0])
                self._model = model
            return self._model

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.model().transcribe(
            audio, language="en", beam_size=2, vad_filter=False, condition_on_previous_text=False,
            no_speech_threshold=0.6, hallucination_silence_threshold=1.0)
        return " ".join(s.text.strip() for s in segments).strip()


class AudioPipeline:
    """One capture thread per source, a shared transcription thread and a turn assembler."""

    def __init__(self, source: str, on_turn: TurnCallback, on_status: StatusCallback,
                 whisper: WhisperService) -> None:
        self.speakers = {"system": ["system"], "microphone": ["microphone"], "both": ["system", "microphone"]}[source]
        self.on_turn, self.on_status, self.whisper = on_turn, on_status, whisper
        self.stop_event = threading.Event()
        self.segments: queue.Queue = queue.Queue(maxsize=6)
        self.turns = TurnAssembler()
        self.threads: list[threading.Thread] = []

    def _device(self, speaker: str):
        import soundcard as sc

        if speaker == "system":
            speaker_device = sc.default_speaker()
            return sc.get_microphone(id=str(speaker_device.id), include_loopback=True), speaker_device.name
        mic = sc.default_microphone()
        return mic, mic.name

    def start(self) -> None:
        self.whisper.model(self.on_status)  # raises if CUDA/model is unavailable
        devices = [(speaker, *self._device(speaker)) for speaker in self.speakers]
        self.on_status("Listening to " + " + ".join(name for _, _, name in devices))
        for speaker, device, _ in devices:
            self.threads.append(threading.Thread(target=self._capture, args=(speaker, device), daemon=True))
        self.threads += [threading.Thread(target=self._transcribe, daemon=True),
                         threading.Thread(target=self._assemble, daemon=True)]
        for thread in self.threads:
            thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)

    def _capture(self, speaker: str, device) -> None:
        import webrtcvad

        segmenter = VadSegmenter(webrtcvad.Vad(config.VAD_MODE).is_speech)
        block = config.RATE * config.CAPTURE_MS // 1000
        try:
            with device.recorder(samplerate=config.RATE) as recorder:
                while not self.stop_event.is_set():
                    audio = np.asarray(recorder.record(numframes=block), dtype=np.float32)
                    if audio.ndim > 1:
                        audio = audio.mean(axis=1)
                    for start in range(0, len(audio) - config.FRAME_SAMPLES + 1, config.FRAME_SAMPLES):
                        segment = segmenter.feed(audio[start:start + config.FRAME_SAMPLES].copy())
                        self.turns.voice(speaker, segmenter.speaking)
                        if segment is not None:
                            self.turns.segment_queued(speaker)
                            try:
                                self.segments.put_nowait((speaker, segment, time.perf_counter()))
                            except queue.Full:
                                self.turns.transcribed(speaker, "", time.perf_counter())
                                self.on_status("Transcription is falling behind; a segment was skipped.")
        except Exception as exc:  # noqa: BLE001 - report device errors to the UI
            self.on_status(f"Audio capture error ({speaker}): {exc}")

    def _transcribe(self) -> None:
        while not self.stop_event.is_set():
            try:
                speaker, audio, ended_at = self.segments.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                text = self.whisper.transcribe(audio)
            except Exception as exc:  # noqa: BLE001
                self.on_status(f"Transcription error: {exc}")
                text = ""
            self.turns.transcribed(speaker, text, ended_at)

    def _assemble(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(0.1)
            for speaker, text, ended_at in self.turns.ready(time.perf_counter()):
                if is_meaningful(text):
                    self.on_turn(speaker, text, ended_at)


def make_pipeline_factory(whisper: WhisperService):
    def factory(source: str, on_turn: TurnCallback, on_status: StatusCallback) -> AudioPipeline:
        return AudioPipeline(source, on_turn, on_status, whisper)
    return factory
