"""Central configuration for Baya.io Companion.

Every value can be overridden with an environment variable so the Windows
prototype can be tuned without editing code.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "static"
DATA_DIR = Path(os.environ.get("BAYA_DATA_DIR", REPO_DIR / "companion_data"))

# --- Network ---------------------------------------------------------------
PORT = _env_int("BAYA_PORT", 8787)
SESSION_HOURS = _env_int("BAYA_SESSION_HOURS", 12)
MAX_PIN_ATTEMPTS_PER_IP = _env_int("BAYA_MAX_PIN_ATTEMPTS", 5)
LOCKOUT_SECONDS = _env_int("BAYA_LOCKOUT_SECONDS", 300)
MAX_FAILED_ATTEMPTS_GLOBAL = _env_int("BAYA_MAX_FAILED_GLOBAL", 20)

# --- Documents ---------------------------------------------------------------
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
MAX_UPLOAD_MB = _env_int("BAYA_MAX_UPLOAD_MB", 25)
CHUNK_CHARS = _env_int("BAYA_CHUNK_CHARS", 900)
CHUNK_OVERLAP_CHARS = _env_int("BAYA_CHUNK_OVERLAP", 150)
TOP_K = _env_int("BAYA_TOP_K", 3)
# A result must share at least this many distinct query terms and reach this
# BM25 score before the assistant treats it as relevant context.
MIN_MATCHED_TERMS = _env_int("BAYA_MIN_MATCHED_TERMS", 2)
MIN_BM25_SCORE = _env_float("BAYA_MIN_BM25_SCORE", 1.5)

# --- Audio (same values as src/baya_v8.py) -----------------------------------
RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = RATE * FRAME_MS // 1000
CAPTURE_MS = 300
VAD_MODE = 2
END_SILENCE_MS = 750
MIN_SPEECH_MS = 300
MAX_SPEECH_SECONDS = 18
MIN_RMS = 0.001
TURN_GAP_S = _env_float("BAYA_TURN_GAP_S", 0.6)
AUDIO_SOURCES = ("system", "microphone", "both")
DEFAULT_AUDIO_SOURCE = "system"

# --- Models -------------------------------------------------------------------
WHISPER_MODEL = os.environ.get("BAYA_WHISPER_MODEL", "base.en")
WHISPER_DEVICE = os.environ.get("BAYA_WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE = os.environ.get("BAYA_WHISPER_COMPUTE", "float16")
OLLAMA_URL = os.environ.get("BAYA_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.environ.get("BAYA_LLM", "qwen2.5:7b")
LLM_MAX_TOKENS = _env_int("BAYA_LLM_MAX_TOKENS", 160)
CUDA_DLL_DIRS = (
    r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\bin",
    r"C:\Program Files\NVIDIA\CUDNN\v9.28\bin\12.9\x64",
)
