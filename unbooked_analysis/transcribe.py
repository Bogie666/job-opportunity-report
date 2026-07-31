"""Whisper transcription via OpenAI API (whisper-1).

No local whisper/ffmpeg on this host. Files are 0.5-3 MB, well under the 25 MB cap.
Key = OPENAI_API_KEY in the vault. response_format=text.
"""
from __future__ import annotations
import os
import requests

OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"


def transcribe(audio_bytes: bytes, filename: str = "call.mp3") -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    if not audio_bytes:
        return None
    files = {"file": (filename, audio_bytes, "audio/mpeg")}
    data = {"model": "whisper-1", "response_format": "text"}
    r = requests.post(OPENAI_URL, headers={"Authorization": f"Bearer {key}"},
                      files=files, data=data, timeout=120)
    if r.status_code != 200:
        return f"[transcription failed: {r.status_code} {r.text[:200]}]"
    return r.text.strip()
