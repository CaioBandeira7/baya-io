import os
import time
import json
import queue
import threading
import collections
import re
import warnings
import difflib
import textwrap
import tkinter as tk
from tkinter import ttk, scrolledtext

import numpy as np
import requests
import soundcard as sc
import webrtcvad

CUDA_BIN = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\bin"
CUDNN_BIN = r"C:\Program Files\NVIDIA\CUDNN\v9.28\bin\12.9\x64"
_dll_handles = []
for path in (CUDA_BIN, CUDNN_BIN):
    if os.path.isdir(path):
        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            _dll_handles.append(os.add_dll_directory(path))

from faster_whisper import WhisperModel

RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = RATE * FRAME_MS // 1000
VAD_MODE = 2
END_SILENCE_MS = 750
MIN_SPEECH_MS = 300
MAX_SPEECH_SECONDS = 18
MIN_RMS = 0.001
WHISPER_MODEL = "base.en"
OLLAMA_MODEL = "llama3.2:3b"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

frame_queue = queue.Queue(maxsize=600)
audio_queue = queue.Queue(maxsize=3)
question_queue = queue.Queue(maxsize=2)
stop_event = threading.Event()

# Demo notes for a FICTITIOUS legal case (all names, dates and amounts are invented).
# Known questions show these notes directly, without asking the AI to invent an answer.
# To use your own material, create a git-ignored "notes.md" next to this script.
NOTES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes.md")

TOPIC_LABELS = {
    "greeting": "GREETING", "case_summary": "CASE SUMMARY", "parties": "PARTIES",
    "facts": "FACTS & TIMELINE", "legal_basis": "LEGAL BASIS", "liability": "ELEMENTS OF LIABILITY",
    "causation": "CAUSAL LINK", "damages": "DAMAGES CLAIMED", "evidence": "EVIDENCE",
    "defense": "EXPECTED DEFENSE", "injunction": "URGENT RELIEF", "jurisdiction": "COURT / VENUE",
    "limitation": "STATUTE OF LIMITATIONS", "settlement": "SETTLEMENT", "procedure": "PROCEDURE",
    "next_steps": "NEXT STEPS", "questions": "OPEN QUESTIONS",
}

# FICTITIOUS demo case: Ana Ribeiro v. Horizonte Logística Ltda. (civil liability, traffic accident).
FALLBACK_NOTES = {
    "greeting": ["Good morning, thanks for joining", "Agenda • Ribeiro v. Horizonte (fictitious demo case)"],
    "case_summary": ["Civil liability claim • traffic accident", "Plaintiff • Ana Ribeiro (fictitious)", "Defendant • Horizonte Logística Ltda. (fictitious)", "Delivery truck rear-ended plaintiff's car", "Claim • material + moral damages + lost income"],
    "parties": ["Plaintiff • Ana Ribeiro, self-employed designer", "Defendant • Horizonte Logística Ltda., carrier company", "Driver was defendant's employee on duty", "Employer liable for employee acts • CC art. 932, III"],
    "facts": ["14 Mar 2025 • collision at a red light", "Truck failed to brake • rear impact", "Police accident report filed same day", "Plaintiff off work for 20 days", "No pre-litigation agreement reached"],
    "legal_basis": ["Unlawful act • Civil Code art. 186", "Duty to compensate • Civil Code art. 927", "Employer liability • Civil Code art. 932, III", "Compensation measured by extent of damage • art. 944", "Complaint requirements • CPC art. 319"],
    "liability": ["Conduct • fault or breach of a legal duty", "Damage • proven harm to a protected interest", "Causal link • conduct directly caused the damage", "Employer liability is strict • CC art. 933"],
    "causation": ["Rear-end collision • presumption of fault", "Police report places truck behind the car", "Dashcam video shows no sudden braking by plaintiff"],
    "damages": ["Material • R$ 18,400 repair estimate", "Lost income • R$ 6,000 (20 days)", "Moral damages requested • R$ 15,000", "Totals are demo figures • not real data"],
    "evidence": ["Police accident report", "Three repair quotes", "Medical certificate • 20 days leave", "Dashcam video + one witness", "Invoices showing average monthly income"],
    "defense": ["Likely argument • plaintiff braked abruptly", "Likely argument • moral damages not proven", "Response • dashcam + rear-end presumption", "Response • leave certificate supports harm"],
    "injunction": ["Urgent relief requirements • CPC art. 300", "Probability of right + risk of harm", "Not requested in this demo case"],
    "jurisdiction": ["Venue • place of the accident or plaintiff's domicile", "CPC art. 53, V • torts from traffic accidents"],
    "limitation": ["Three-year limitation period", "Civil Code art. 206, §3º, V", "Accident Mar 2025 • deadline Mar 2028"],
    "settlement": ["Conciliation hearing • CPC art. 334", "Open to settle on material damages first", "Confirm client's minimum before any offer"],
    "procedure": ["Complaint filed → service of process", "Conciliation hearing", "Defense • 15 business days", "Evidence phase → judgment"],
    "next_steps": ["Collect final repair invoice", "Request witness statement in writing", "Draft complaint for client review"],
    "questions": ["Did the client receive any insurance payment?", "Any prior damage to the car?"],
}

QUESTION_PATTERNS = {
    "greeting": ["how are you doing", "how are you today", "how's it going"],
    "case_summary": ["summarize the case", "what is this case about", "case overview", "brief me on the case"],
    "parties": ["who are the parties", "who is the plaintiff", "who is the defendant", "who are we suing"],
    "facts": ["what happened", "facts of the case", "walk me through the facts", "timeline of events"],
    "legal_basis": ["legal basis", "which articles", "what law applies", "legal grounds"],
    "liability": ["elements of liability", "what must be proven", "civil liability", "held responsible"],
    "causation": ["causal link", "causation", "who caused the accident"],
    "damages": ["what damages", "how much are we claiming", "moral damages", "material damages", "amount of the claim"],
    "evidence": ["what evidence", "what proof", "which documents", "do we have proof"],
    "defense": ["defendant argue", "their defense", "counterarguments", "weak points"],
    "injunction": ["urgent relief", "injunction", "preliminary relief"],
    "jurisdiction": ["which court", "jurisdiction", "where should we file", "venue"],
    "limitation": ["statute of limitations", "limitation period", "is it time barred", "deadline to file"],
    "settlement": ["settlement", "settle the case", "conciliation", "agreement with the defendant"],
    "procedure": ["procedural steps", "how does the procedure work", "stages of the lawsuit"],
    "next_steps": ["next steps", "what happens next", "what should we do now"],
    "questions": ["questions for me", "questions for us", "any questions", "anything else you need"],
}

def normalized(text):
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).replace("  ", " ").strip()

def match_topic(question):
    q = normalized(question)
    matches = []
    for topic, patterns in QUESTION_PATTERNS.items():
        for pattern in patterns:
            p = normalized(pattern)
            if p in q:
                matches.append((len(p), topic))
    if matches:
        return max(matches)[1]
    # Fuzzy matching is only used at a high confidence threshold.
    options = [(difflib.SequenceMatcher(None, q, normalized(pattern)).ratio(), topic)
               for topic, patterns in QUESTION_PATTERNS.items() for pattern in patterns]
    score, topic = max(options)
    return topic if score >= 0.79 else None

def load_document_notes():
    # Markdown table columns contain the user's own response fragments.
    # Prefer the user's exact wording over the fallback notes where possible.
    if not os.path.isfile(NOTES_FILE):
        return FALLBACK_NOTES
    try:
        raw = open(NOTES_FILE, encoding="utf-8").read()
        sections = re.split(r"(?m)^#{2,4}\s+", raw)
        notes = dict(FALLBACK_NOTES)
        for section in sections:
            heading = section.splitlines()[0].replace("**", "").strip() if section.strip() else ""
            topic = match_topic(heading)
            if not topic:
                continue
            chunks = []
            for line in section.splitlines()[1:]:
                if not line.startswith("|") or re.match(r"^\|[- :|]+\|$", line):
                    continue
                for cell in line.strip().strip("|").split("|"):
                    cell = re.sub(r"<br\s*/?>", " ", cell, flags=re.I)
                    cell = re.sub(r"\*\*|<[^>]+>", "", cell).strip()
                    if len(cell) >= 8 and not cell.startswith("-"):
                        chunks.append(cell)
            if chunks:
                # A small number of concise reminders, not a complete speech.
                notes[topic] = chunks[:8]
        return notes
    except (OSError, UnicodeError) as exc:
        print("[NOTES] Could not read Markdown:", exc, flush=True)
        return FALLBACK_NOTES

DOCUMENT_NOTES = load_document_notes()

# GUI state. Tkinter is updated only on the main thread.
ui_events = queue.Queue()
whisper = None
speaker = None
loopback = None
started = False

def emit(kind, *values):
    ui_events.put((kind, *values))

def initialize_and_start():
    global whisper, speaker, loopback
    try:
        emit("status", "Loading Whisper on GPU...")
        whisper = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16", cpu_threads=4)
        speaker = sc.default_speaker()
        loopback = sc.get_microphone(id=str(speaker.id), include_loopback=True)
        emit("device", speaker.name)
        for target in (capture_frames, segment_speech, transcribe_audio, generate_answers):
            threading.Thread(target=target, daemon=True).start()
        emit("status", "Listening to PC audio • Ready")
    except Exception as exc:
        emit("status", "Startup failed")
        emit("error", f"Initialization error: {exc}")


def put_latest(q, item):
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


def capture_frames():
    # Read 300 ms from WASAPI and split into 30 ms VAD frames.
    # This reduces driver calls; it does not guarantee no discontinuities.
    capture_samples = RATE * 300 // 1000
    dropped = 0
    try:
        with loopback.recorder(samplerate=RATE) as recorder:
            while not stop_event.is_set():
                audio = np.asarray(recorder.record(numframes=capture_samples), dtype=np.float32)
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                for start in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
                    frame = audio[start:start + FRAME_SAMPLES].copy()
                    if frame_queue.full():
                        dropped += 1
                        if dropped == 1 or dropped % 100 == 0:
                            print(f"[WARNING] VAD input queue overflow; dropped frames: {dropped}", flush=True)
                    put_latest(frame_queue, frame)
    except Exception as exc:
        emit("error", f"Capture error: {exc}")
        stop_event.set()


def segment_speech():
    vad = webrtcvad.Vad(VAD_MODE)
    pre_roll = collections.deque(maxlen=10)
    speaking = False
    frames = []
    voiced_frames = 0
    silent_frames = 0
    silence_limit = max(1, END_SILENCE_MS // FRAME_MS)
    max_frames = int(MAX_SPEECH_SECONDS * 1000 / FRAME_MS)
    while not stop_event.is_set():
        try:
            audio = frame_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        audio = np.clip(audio, -1, 1)
        rms = float(np.sqrt(np.mean(audio * audio)))
        pcm = (audio * 32767).astype(np.int16).tobytes()
        voiced = rms >= MIN_RMS and vad.is_speech(pcm, RATE)
        if not speaking:
            pre_roll.append(audio)
            if voiced:
                speaking = True
                frames = list(pre_roll)
                pre_roll.clear()
                voiced_frames = 1
                silent_frames = 0
                emit("status", "Voice detected • Listening...")
        else:
            frames.append(audio)
            if voiced:
                voiced_frames += 1
                silent_frames = 0
            else:
                silent_frames += 1
            if silent_frames >= silence_limit or len(frames) >= max_frames:
                duration = len(frames) * FRAME_MS / 1000
                if voiced_frames * FRAME_MS >= MIN_SPEECH_MS:
                    put_latest(audio_queue, (np.concatenate(frames).astype(np.float32), time.perf_counter()))
                    emit("status", "Transcribing...")
                speaking = False
                frames = []
                voiced_frames = 0
                silent_frames = 0
                pre_roll.clear()


def classify_question(text):
    q = text.lower()
    if any(x in q for x in ("salary", "compensation", "pay expectation", "expected pay")):
        return "SALARY"
    if any(x in q for x in ("when can you start", "notice period", "availability", "start date")):
        return "AVAILABILITY"
    if any(x in q for x in ("any questions for us", "any questions for me", "questions for us", "questions for me")):
        return "ASK_INTERVIEWER"
    if any(x in q for x in ("does that sound good", "does that work for you", "is that okay with you")):
        return "CONFIRMATION"
    if any(x in q for x in ("why did you leave", "why are you leaving", "why did you stop", "why did you close", "why did you shut down")):
        return "LEAVING"
    if any(x in q for x in ("challenge", "difficult situation", "problem you faced", "biggest obstacle")):
        return "CHALLENGE"
    if any(x in q for x in ("failure", "mistake", "didn't work", "did not work")):
        return "FAILURE"
    if any(x in q for x in ("weakness", "area for improvement")):
        return "WEAKNESS"
    if any(x in q for x in ("strength", "good at")):
        return "STRENGTH"
    if any(x in q for x in ("interested in this role", "interested in this room", "why this position", "why do you want this job", "why this role", "why do you want to work")):
        return "MOTIVATION"
    if any(x in q for x in ("tell me about yourself", "talk about yourself", "walk me through your background", "introduce yourself")):
        return "INTRODUCTION"
    return "GENERAL"


def is_question(text):
    q = re.sub(r"\s+", " ", text.lower()).strip()
    if len(q.split()) < 3 or q.strip(" .?!,") in {"thank you", "good morning", "all right", "let's go"}:
        return False
    if "?" in q:
        return True
    if any(x in q for x in ("tell me about yourself", "talk about yourself", "describe your experience", "walk me through")):
        return True
    return q.startswith(("what ", "why ", "how ", "when ", "where ", "who ", "which ", "can you ", "could you ", "do you ", "have you ", "would you ", "are you ", "did you ", "describe ", "explain "))


def transcribe_audio():
    while not stop_event.is_set():
        try:
            audio, ended_at = audio_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        started = time.perf_counter()
        try:
            segments, _ = whisper.transcribe(audio, language="en", beam_size=2,
                vad_filter=False, condition_on_previous_text=False,
                no_speech_threshold=0.6, hallucination_silence_threshold=1.0)
            text = " ".join(seg.text.strip() for seg in segments).strip()
        except Exception as exc:
            emit("error", f"Whisper error: {exc}")
            continue
        emit("transcript", text, time.perf_counter()-started)
        if text and is_question(text):
            put_latest(question_queue, (text, ended_at))


def make_bullet_prompt(question):
    return f"""You are a question interpreter for a live meeting assistant.
The user's prepared notes are below. Use ONLY these notes.
Your task is NOT to write an answer. Identify the question's intent and
return 3-5 very short English keyword bullets that act as speaking cues.
Each bullet must be 3-10 words. Do not invent facts, dates, numbers,
experiences, methods or results. If notes do not support the question,
write exactly: No prepared notes for this question.

NOTES:
{json.dumps(DOCUMENT_NOTES, ensure_ascii=False)}

QUESTION: {question}
OUTPUT FORMAT (no paragraphs):
- short speaking cue
- short speaking cue
"""


def generate_answers():
    session = requests.Session()
    while not stop_event.is_set():
        try:
            question, speech_ended_at = question_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        topic = match_topic(question)
        emit("question", question, TOPIC_LABELS.get(topic, "OTHER QUESTION"))
        start = time.perf_counter()
        if topic:
            bullets = DOCUMENT_NOTES.get(topic, FALLBACK_NOTES.get(topic, []))
            display = "\n\n".join("• " + item.strip() for item in bullets)
            emit("answer", display, time.perf_counter() - speech_ended_at, 0.0)
            emit("status", "Speaking cues ready • Listening...")
            continue
        emit("status", "Interpreting new question...")
        try:
            with session.post(OLLAMA_URL, json={
                "model": OLLAMA_MODEL,
                "prompt": make_bullet_prompt(question),
                "stream": False,
                "keep_alive": "30m",
                "options": {"temperature": 0.0, "num_predict": 140, "num_ctx": 4096}
            }, timeout=(5, 120)) as response:
                response.raise_for_status()
                data = response.json()
            answer = data.get("response", "").strip()
            lines = [x.strip() for x in answer.splitlines() if x.strip().startswith(("- ", "• "))]
            if lines:
                answer = "\n\n".join("• " + re.sub(r"^[-•]\s*", "", x) for x in lines[:5])
            else:
                answer = "No prepared notes for this question."
            emit("answer", answer, time.perf_counter()-speech_ended_at, time.perf_counter()-start)
            emit("status", "Speaking cues ready • Listening...")
        except (requests.RequestException, ValueError) as exc:
            emit("error", f"Ollama error: {exc}")

# ------------------- Desktop overlay -------------------
root = tk.Tk()
root.title("Baya.io • Speaking Cues")
root.geometry("780x530+80+80")
root.minsize(560, 400)
root.configure(bg="#111827")
root.attributes("-topmost", True)

status_var = tk.StringVar(value="Ready to start")
device_var = tk.StringVar(value="PC audio • WASAPI loopback")
metric_var = tk.StringVar(value="Waiting for a question")
top_var = tk.BooleanVar(value=True)

header = tk.Frame(root, bg="#111827")
header.pack(fill="x", padx=18, pady=(16, 7))
tk.Label(header, text="BAYA.IO • SPEAKING CUES", fg="#f8fafc", bg="#111827", font=("Segoe UI", 17, "bold")).pack(anchor="w")
tk.Label(header, text="Your prepared notes • keywords, not scripts", fg="#93a4b8", bg="#111827", font=("Segoe UI", 10)).pack(anchor="w")

controls = tk.Frame(root, bg="#111827")
controls.pack(fill="x", padx=18, pady=(4, 10))

def start_app():
    global started
    if started:
        return
    started = True
    start_button.config(state="disabled", text="Running")
    threading.Thread(target=initialize_and_start, daemon=True).start()

def close_app():
    stop_event.set()
    root.destroy()

def toggle_top():
    root.attributes("-topmost", top_var.get())

start_button = tk.Button(controls, text="Start listening", command=start_app, bg="#2563eb", fg="white", activebackground="#1d4ed8", activeforeground="white", font=("Segoe UI", 10, "bold"), relief="flat", padx=15, pady=9)
start_button.pack(side="left")
tk.Button(controls, text="Stop / Exit", command=close_app, bg="#334155", fg="white", relief="flat", padx=12, pady=9).pack(side="left", padx=8)
tk.Checkbutton(controls, text="Always on top", variable=top_var, command=toggle_top, bg="#111827", fg="#e2e8f0", selectcolor="#334155", activebackground="#111827", activeforeground="white").pack(side="right")

tk.Label(root, textvariable=status_var, bg="#111827", fg="#67e8f9", font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", padx=18)
tk.Label(root, textvariable=device_var, bg="#111827", fg="#94a3b8", font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=18, pady=(0, 12))

def add_text_area(label, height, fg):
    tk.Label(root, text=label, bg="#111827", fg="#94a3b8", font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", padx=18, pady=(7, 4))
    area = scrolledtext.ScrolledText(root, wrap="word", height=height, bg="#1e293b", fg=fg, insertbackground="white", relief="flat", borderwidth=0, padx=13, pady=12, font=("Segoe UI", 12), state="disabled")
    area.pack(fill="both", expand=True, padx=18, pady=(0, 4))
    return area

question_area = add_text_area("DETECTED QUESTION", 3, "#f1f5f9")
answer_area = add_text_area("YOUR SPEAKING CUES", 7, "#c4f1d0")
answer_area.configure(font=("Segoe UI", 14), spacing2=5, spacing3=10)
tk.Label(root, textvariable=metric_var, bg="#111827", fg="#94a3b8", font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=18, pady=(6, 12))

def format_spoken_answer(value):
    # Visual line breaks between sentences help reading out loud.
    # Never change the words generated by the model.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", value.strip())
    return "\n\n".join(parts)


def set_text(widget, value):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", value)
    widget.configure(state="disabled")

def poll_ui():
    try:
        for _ in range(60):
            event = ui_events.get_nowait()
            kind, *args = event
            if kind == "status":
                status_var.set(args[0])
            elif kind == "device":
                device_var.set("Audio device: " + args[0])
            elif kind == "transcript":
                metric_var.set(f"Whisper: {args[1]:.2f}s • Transcribed: {args[0]}")
            elif kind == "question":
                set_text(question_area, args[0])
                set_text(answer_area, "Generating...")
                metric_var.set("Category: " + args[1])
            elif kind == "answer":
                set_text(answer_area, args[0])
                metric_var.set(f"After speech: {args[1]:.2f}s • " + ("Your notes • instant" if args[2] == 0 else f"AI interpretation: {args[2]:.2f}s"))
            elif kind == "error":
                status_var.set(args[0])
    except queue.Empty:
        pass
    if not stop_event.is_set():
        root.after(70, poll_ui)

root.protocol("WM_DELETE_WINDOW", close_app)
root.after(70, poll_ui)
root.mainloop()
