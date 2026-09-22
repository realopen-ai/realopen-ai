"""Persistent user-facing voice assistant settings."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict

from app.config import settings

PERSONAS = {
    "friendly": "Be warm, natural, and encouraging. Sound like a helpful friend.",
    "concise": "Be direct and brief. Give the answer first and omit unrequested detail.",
    "playful": "Be lively and lightly playful, while staying useful and respectful.",
    "technical": "Be precise and technical. Explain important reasoning and tradeoffs clearly.",
}
SPEEDS = {0.5, 1.0, 1.5, 2.0}
_lock = threading.Lock()


def _path() -> Path:
    path = Path("/app/data/voice-settings.json")
    if not path.parent.exists():
        path = Path(__file__).resolve().parents[3] / "data" / "voice-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def defaults() -> Dict[str, Any]:
    return {
        "voice": str(settings.get_voice_config().tts.voice or "mary"),
        "speed": 1.0,
        "persona": "friendly",
        "custom_personas": [],
    }


def get() -> Dict[str, Any]:
    result = defaults()
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            result.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    return result


def update(payload: Dict[str, Any]) -> Dict[str, Any]:
    current = get()
    if "voice" in payload:
        voice = str(payload["voice"]).strip()
        if not voice or len(voice) > 160:
            raise ValueError("Invalid voice")
        current["voice"] = voice
    if "speed" in payload:
        speed = float(payload["speed"])
        if speed not in SPEEDS:
            raise ValueError("Speed must be 0.5, 1, 1.5, or 2")
        current["speed"] = speed
    if "custom_personas" in payload:
        values = payload["custom_personas"]
        if not isinstance(values, list) or len(values) > 20:
            raise ValueError("custom_personas must be a list of at most 20 items")
        clean = []
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("Invalid custom persona")
            ident = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            prompt = str(item.get("prompt", "")).strip()
            if not ident.startswith("custom-") or not name or not prompt or len(prompt) > 2000:
                raise ValueError("Invalid custom persona")
            clean.append({"id": ident[:80], "name": name[:80], "prompt": prompt})
        current["custom_personas"] = clean
        valid = set(PERSONAS) | {p["id"] for p in clean}
        if current["persona"] not in valid:
            current["persona"] = "friendly"
    if "persona" in payload:
        persona = str(payload["persona"]).strip()
        valid = set(PERSONAS) | {p.get("id") for p in current["custom_personas"]}
        if persona not in valid:
            raise ValueError("Unknown persona")
        current["persona"] = persona
    with _lock:
        _path().write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def persona_prompt() -> str:
    current = get()
    selected = current["persona"]
    if selected in PERSONAS:
        return PERSONAS[selected]
    for persona in current["custom_personas"]:
        if persona["id"] == selected:
            return persona["prompt"]
    return PERSONAS["friendly"]
