# Baya.io Companion: local second screen

> Status: 🚧 MVP, work in progress. Intended for **authorized** meetings, presentations and personal study.

The Companion keeps the presentation screen clean: the Windows PC does all the processing (audio capture, Faster-Whisper, document search, Ollama), and an iPad on the same Wi-Fi shows the live transcription and relevant passages from your own documents in Safari. There is no native app and no cloud service.

```
Windows PC                                              iPad (Safari)
audio ─► VAD ─► Faster-Whisper ─► turns ─┐             ● LISTENING banner
                                          ├─► SSE ───►  live transcription
documents ─► chunks ─► BM25 ─► Ollama ───┘             context + sources + excerpts
FastAPI: PIN pairing, local-network filter              documents · Start / Stop
```

## What it does

| Feature | Implementation |
|---|---|
| Audio source | System audio (default), microphone, or both. Capture is **off** until Start is pressed. |
| Transcription | Same VAD and Faster-Whisper settings as `src/baya_v8.py`; segments are merged into complete turns. |
| Document library | PDF, DOCX, TXT and Markdown, up to 25 MB each. Text is split into passages with page / section / line locations. |
| Retrieval | BM25 keyword search over the **active** documents (lexical, not semantic). |
| Context | For relevant turns, the local model writes 1–3 sentences that must cite the numbered sources. The original excerpts are always shown. |
| Verification | Citations to non-existent sources are removed; numbers that do not appear in the excerpts are flagged. |
| Delete / deactivate | Removes the passages from the index, clears the search cache and removes every context card that cited the document, including an answer still being generated. |
| Stop | Stops capture and clears the transcript and context. Audio is never written to disk. |

## Setup (Windows)

Prerequisites are the same as the V8 prototype (NVIDIA GPU with CUDA 12 and cuDNN 9, Ollama), plus the Companion packages:

```powershell
cd baya-io
C:\Users\<you>\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-companion.txt
ollama pull qwen2.5:7b
```

## Run

```powershell
start_companion.bat            # or: python -m companion
```

The console and a small always-on-top window on the PC show:

```
Open on the iPad (same Wi-Fi):  http://192.168.x.x:8787
Pairing PIN:                    123456
```

1. The first time, Windows Firewall asks whether to allow Python: allow it on **Private networks only**.
2. **PC = control panel.** The Companion opens `/control` in the PC's browser with a one-time link (no PIN on the PC; the status window has an *Open control panel* button to reopen it). Add documents, choose the audio source and Start/Stop the session there.
3. **iPad = results display.** Open the address in Safari (type `http://` explicitly) and enter the PIN. The iPad shows `/display`: the recording indicator, the latest context in large type, older cards dimmed, and live captions at the bottom, with no controls. Optional: Share → *Add to Home Screen*.

The one-time launch link only works from the PC itself (matched against the address the server is bound to, not the `Host` header) and only once.

Options: `--port 8787`, `--host <LAN IP>` (if the wrong network adapter is detected), `--no-llm` (excerpts only), `--no-window`. Environment variables are listed in `companion/config.py` (for example `BAYA_LLM` to choose another Ollama model).

## Security and privacy

- **Local network only.** The server binds to the PC's private address and rejects any client outside private ranges (10/8, 172.16/12, 192.168/16, loopback, IPv6 ULA). It refuses to start on a public address. Do not set up port forwarding or tunnels.
- **Authentication on every route**, including the live event stream and static scripts. Only the login page, its script, stylesheet and logo are public; they contain no data.
- **Pairing PIN** (6 digits, new on every start). It is exchanged for a random session token stored in an `HttpOnly`, `SameSite=Strict` cookie for 12 hours. 5 wrong PINs lock a device out for 5 minutes; 20 wrong PINs across devices rotate the PIN.
- **Cross-site protection.** State-changing requests must come from the Companion's own origin.
- **Security headers.** CSP with `default-src 'self'`, no framing, no referrer, no caching of API responses.
- **Uploads.** Extension and file signature are checked; names are sanitized and files are stored under generated IDs in `companion_data/` (git-ignored).
- **Limitation: no HTTPS.** Traffic on the Wi-Fi is plain HTTP. Use the Companion only on a trusted home or office network, never on public Wi-Fi.
- **No concealment.** A red LISTENING banner and border on the iPad and a status window on the PC are always visible while capturing.

## Limitations

- Transcription is English-only (`base.en`) and needs an NVIDIA GPU.
- Retrieval is keyword-based: paraphrases with different words may be missed. Embedding-based retrieval is planned.
- Scanned PDFs without a text layer and password-protected PDFs are rejected (no OCR).
- The summary comes from a local language model and can still be wrong; always check the excerpt.

## Tests

```powershell
python -m pytest tests -q
```

91 tests cover real PDF, DOCX, TXT (Windows-1252) and Markdown files, rejected files (empty, fake PDF, corrupted PDF, scanned PDF, encrypted PDF, oversized, binary as text, path traversal names, duplicates), BM25 ranking, activation and deletion invalidation (index, cache, files, session context, answers in flight), authentication on every route including SSE, rate limiting, the local-network filter, the PC control / iPad display routing and one-time launch links, CSRF protection, the session lifecycle, citation verification, and the VAD and turn-assembly logic without audio hardware.
