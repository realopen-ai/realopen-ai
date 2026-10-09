"""Model providers (local Ollama + cloud Groq).

This module is the single place that knows WHERE a chat request should be
sent. Every model ID in the app resolves to a provider:

- Ollama models (``qwen3:4b``, ``x/flux2-klein:4b``, …) → local inference
  via ``{OLLAMA_BASE_URL}/api/chat``.
- Groq models (``openai/gpt-oss-120b``, ``qwen/qwen3.6-27b``, …) → cloud
  inference via ``https://api.groq.com/openai/v1/chat/completions`` using
  the API key saved by the Settings ▸ AI ▸ Providers UI.

Public surface used by the rest of the backend:

- ``ollama_status()``           — connected flag + installed model count
- ``groq_status()``             — key present/connected + masked key
- ``test_groq_key()``           — mini request to Groq to validate a key
- ``set_groq_key()/clear_groq_key()``
- ``is_groq_model()``           — provider lookup by model id
- ``list_available_models()``   — merged dropdown list (Ollama + Groq)
- ``chat_once()``               — non-streaming chat, provider-routed,
                                  always returns an Ollama-shaped dict
- ``stream_chat()``             — streaming chat, provider-routed, yields
                                  NORMALIZED chunks (ollama/groq unified)

The Groq API key is persisted at ``state/groq_api_key`` (never returned in
clear text by any endpoint — only a masked form).
"""

import json
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

from app.config import settings

# ─── Groq constants ─────────────────────────────────────────────────

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Cloud models offered when the Groq provider is connected. Kept static so
# the dropdown works even before the key is used for anything.
GROQ_MODELS: List[Dict[str, str]] = [
    {
        "id": "qwen/qwen3.6-27b",
        "description": "Qwen 3.6 27B",
        "type": "chat",
        "size": "cloud",
    },
    {
        "id": "qwen/qwen3.8-27b",
        "description": "Qwen 3.8 27B",
        "type": "chat",
        "size": "cloud",
    },
    {
        "id": "openai/gpt-oss-20b",
        "description": "GPT OSS 20B",
        "type": "chat",
        "size": "cloud",
    },
    {
        "id": "openai/gpt-oss-120b",
        "description": "GPT OSS 120B",
        "type": "chat",
        "size": "cloud",
    },
]

_GROQ_IDS = {m["id"] for m in GROQ_MODELS}

# ─── Ollama tags cache (avoids hammering /api/tags on every lookup) ──

_TAGS_TTL_SECONDS = 30.0
_tags_cache: Dict[str, Any] = {"ts": 0.0, "models": [], "reachable": False}
_tags_lock = None  # created lazily (asyncio.Lock must be made in a loop)


def _get_tags_lock():
    global _tags_lock
    if _tags_lock is None:
        import asyncio

        _tags_lock = asyncio.Lock()
    return _tags_lock


# ─── State directory (same convention as config._get_state_dir) ─────


def _state_dir() -> Path:
    """Persistent runtime state dir (Docker: /app/state, dev: ../state)."""
    return settings._get_state_dir()


def _groq_key_path() -> Path:
    return _state_dir() / "groq_api_key"


# ─── Provider lookup ────────────────────────────────────────────────


def is_groq_model(model_id: str) -> bool:
    """True when the model id belongs to the Groq cloud provider."""
    return model_id in _GROQ_IDS


def provider_of(model_id: str) -> str:
    """'groq' | 'ollama' for any model id."""
    return "groq" if is_groq_model(model_id) else "ollama"


# ─── Ollama status + installed models ───────────────────────────────


def _human_size(num_bytes: Optional[float]) -> str:
    if not num_bytes:
        return ""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}" if unit not in ("B",) else f"{int(size)} B"
        size /= 1024.0
    return ""


# Publisher namespaces that add no information to the display name
# ("openai/gpt-oss-120b" → "GPT OSS 120B", not "OpenAI / GPT OSS 120B").
_DROP_PUBLISHERS = {
    "openai",
    "qwen",
    "meta",
    "mistralai",
    "google",
    "library",
    "ollama",
}

# Tokens conventionally rendered in caps
_UPPER_TOKENS = {
    "gpt": "GPT",
    "oss": "OSS",
    "mlx": "MLX",
    "vl": "VL",
    "ai": "AI",
    "glm": "GLM",
    "sd": "SD",
    "sdxl": "SDXL",
    "sd3": "SD3",
    "llava": "LLaVA",
    "bf16": "BF16",
    "f16": "F16",
    "f32": "F32",
    "f8": "F8",
}


def _split_num_boundary(part: str) -> str:
    # "qwen3.5" → "qwen 3.5", "flux2" → "flux 2" (but "v1.5" stays whole)
    if re.match(r"^[vV]\d", part):
        return part
    return re.sub(r"(?<=[a-z])(?=\d)", " ", part)


def pretty_model_name(model_id: str) -> str:
    """Human-readable label for a raw model id.

    "qwen3.5:4b-mlx"       → "Qwen 3.5 4B MLX"
    "x/flux2-klein:4b"     → "X / Flux 2 Klein 4B"
    "openai/gpt-oss-120b"  → "GPT OSS 120B"
    "qwen3:32b"            → "Qwen 3 32B"
    """
    publisher = ""
    name = model_id
    if "/" in model_id:
        publisher, name = model_id.split("/", 1)
        if publisher.lower() in _DROP_PUBLISHERS:
            publisher = ""
    tag = ""
    if ":" in name:
        name, tag = name.split(":", 1)

    def _title(part: str) -> str:
        out = []
        for w in part.split("-"):
            if not w:
                continue
            low = w.lower()
            if low in _UPPER_TOKENS:
                out.append(_UPPER_TOKENS[low])
            elif re.match(r"^\d+[a-z]$", low):  # 4b / 13b / 120b
                out.append(low.upper())
            elif re.match(r"^[qfba]\d+_\w+$", low):  # q4_k_m quant tags
                out.append(low.upper())
            else:
                out.append(_split_num_boundary(w).title())
        return " ".join(out)

    label = _title(name)
    if tag:
        label += " " + _title(tag)
    if publisher:
        pub = publisher.upper() if len(publisher) == 1 else publisher.title()
        label = f"{pub} / {label}"
    return label


async def _fetch_ollama_tags(force: bool = False) -> List[Dict[str, Any]]:
    """List models installed in Ollama (GET /api/tags), TTL-cached."""
    async with _get_tags_lock():
        now = time.time()
        if not force and (now - _tags_cache["ts"]) < _TAGS_TTL_SECONDS:
            return _tags_cache["models"]
        models: List[Dict[str, Any]] = []
        reachable = False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
                if resp.status_code == 200:
                    reachable = True
                    for m in resp.json().get("models", []):
                        details = m.get("details", {}) or {}
                        models.append(
                            {
                                "id": m.get("name", ""),
                                "provider": "ollama",
                                "installed": True,
                                "description": pretty_model_name(m.get("name", "")),
                                "size": _human_size(m.get("size")),
                                "type": details.get("family", "chat") or "chat",
                                "modified_at": m.get("modified_at", ""),
                            }
                        )
        except Exception:
            models = []
        _tags_cache["ts"] = time.time()
        _tags_cache["models"] = models
        _tags_cache["reachable"] = reachable
        return models


async def ollama_status() -> Dict[str, Any]:
    """Connection status for the local Ollama provider."""
    models = await _fetch_ollama_tags()
    connected = bool(models) or await _ollama_reachable()
    return {
        "id": "ollama",
        "name": "Ollama",
        "kind": "local",
        "connected": connected,
        "model_count": len(models),
    }


async def _ollama_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{settings.OLLAMA_BASE_URL}/api/version")
            return resp.status_code == 200
    except Exception:
        return False


# ─── Groq key management ────────────────────────────────────────────


def get_groq_key() -> Optional[str]:
    """Read the saved Groq API key (or None)."""
    path = _groq_key_path()
    try:
        if path.exists():
            key = path.read_text(encoding="utf-8").strip()
            return key or None
    except OSError:
        pass
    return None


def _mask_key(key: str) -> str:
    if len(key) <= 10:
        return "•" * max(8, len(key))
    return f"{key[:4]}{'•' * 8}{key[-4:]}"


async def test_groq_key(api_key: str) -> Tuple[bool, str]:
    """Validate an API key with a mini request to Groq.

    Sends GET /openai/v1/models with the Bearer key — cheap, no tokens
    generated. Returns (ok, detail).
    """
    api_key = (api_key or "").strip()
    if not api_key:
        return False, "API key is empty"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{GROQ_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                return True, "ok"
            if resp.status_code in (401, 403):
                return False, "Invalid API key — Groq rejected it (401)"
            return False, f"Groq returned HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, "Timed out contacting Groq — check your connection"
    except httpx.ConnectError:
        return False, "Cannot reach Groq — check your connection"
    except Exception as e:  # pragma: no cover - defensive
        return False, f"Unexpected error validating key: {e}"


def set_groq_key(api_key: str) -> None:
    """Persist the Groq API key (called only after validation)."""
    path = _groq_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(api_key.strip() + "\n", encoding="utf-8")
    try:  # best-effort permission tightening
        path.chmod(0o600)
    except OSError:
        pass


def clear_groq_key() -> bool:
    """Remove the saved key. Returns True when a key existed."""
    path = _groq_key_path()
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


async def groq_status() -> Dict[str, Any]:
    """Status of the Groq cloud provider for the Providers UI."""
    key = get_groq_key()
    connected = False
    if key:
        ok, _ = await test_groq_key(key)
        connected = ok
    status: Dict[str, Any] = {
        "id": "groq",
        "name": "Groq",
        "kind": "cloud",
        "connected": connected,
        "model_count": len(GROQ_MODELS) if connected else 0,
        "has_key": key is not None,
    }
    if key:
        status["key_masked"] = _mask_key(key)
    return status


# ─── Merged dropdown model list ─────────────────────────────────────


async def list_available_models() -> Dict[str, Any]:
    """Everything the Settings ▸ AI ▸ Models dropdowns need, in one call.

    Merges:
      - models installed in Ollama (live /api/tags)
      - the current profile's models (so defaults are selectable even
        when not pulled yet — flagged installed: false)
      - Groq cloud models (only when the provider is connected)
    """
    installed = await _fetch_ollama_tags()
    installed_ids = {m["id"] for m in installed}

    # Profile + module models (defaults from setup) that aren't installed
    profile_models: List[Dict[str, Any]] = []
    for m in settings.get_available_models():
        mid = m.get("id", "")
        if mid and mid not in installed_ids:
            profile_models.append(
                {
                    "id": mid,
                    "provider": "ollama",
                    "installed": False,
                    "description": m.get("description") or pretty_model_name(mid),
                    "size": m.get("size", ""),
                    "type": m.get("type", "chat"),
                }
            )

    groq = await groq_status()
    cloud: List[Dict[str, Any]] = []
    if groq["connected"]:
        cloud = [
            {
                "id": m["id"],
                "provider": "groq",
                "installed": True,
                "description": m["description"],
                "size": m.get("size", ""),
                "type": m.get("type", "chat"),
            }
            for m in GROQ_MODELS
        ]

    return {
        "models": installed + profile_models + cloud,
        "groq_connected": groq["connected"],
        "ollama_connected": _tags_cache.get("reachable", False),
    }


# ─── Provider-routed chat calls ─────────────────────────────────────


def _groq_headers() -> Dict[str, str]:
    key = get_groq_key()
    if not key:
        raise RuntimeError("Groq model selected but no API key is saved")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _to_groq_messages(messages: List[dict]) -> List[dict]:
    """Convert Ollama-style messages to OpenAI-compatible content parts.

    Ollama vision messages carry base64 images as {"images": [b64, …]}.
    Groq expects content parts with data URLs.
    """
    converted: List[dict] = []
    for m in messages:
        images = m.get("images")
        if not images:
            converted.append({"role": m["role"], "content": m.get("content", "")})
            continue
        parts: List[dict] = []
        text = m.get("content", "")
        if text:
            parts.append({"type": "text", "text": text})
        for b64 in images:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                }
            )
        converted.append({"role": m["role"], "content": parts})
    return converted


def _groq_payload(
    model: str,
    messages: List[dict],
    stream: bool,
    tools: Optional[List[dict]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Build a Groq chat/completions payload from Ollama-style kwargs.

    Mapping: options.num_predict → max_tokens, options.temperature →
    temperature, format:"json" → response_format, "think" dropped.
    """
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _to_groq_messages(messages),
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
    options = (
        (kwargs.get("options") or {}) if isinstance(kwargs.get("options"), dict) else {}
    )
    if options.get("num_predict") is not None:
        payload["max_tokens"] = int(options["num_predict"])
    if options.get("temperature") is not None:
        payload["temperature"] = float(options["temperature"])
    if kwargs.get("format") == "json":
        payload["response_format"] = {"type": "json_object"}
    return payload


async def chat_once(
    model: str,
    messages: List[dict],
    tools: Optional[List[dict]] = None,
    timeout: float = 600.0,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Non-streaming chat call routed by provider.

    ALWAYS returns an Ollama-shaped dict:
      {"message": {"role": "assistant", "content": str},
       "model": str, "done": True, …}
    so existing ``data.get("message", {}).get("content")`` parsing keeps
    working regardless of the provider.
    """
    if is_groq_model(model):
        payload = _groq_payload(model, messages, stream=False, tools=tools, **kwargs)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{GROQ_BASE_URL}/chat/completions",
                json=payload,
                headers=_groq_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        return {
            "message": {
                "role": "assistant",
                "content": msg.get("content", ""),
            },
            "model": data.get("model", model),
            "done": True,
            "provider": "groq",
        }

    # Ollama path — kwargs pass through unchanged (think/format/options)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    payload.update(kwargs)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{settings.OLLAMA_BASE_URL}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
    data.setdefault("done", True)
    data.setdefault("provider", "ollama")
    return data


class _GroqToolCallAccumulator:
    """Assembles OpenAI-style streamed tool-call fragments into complete
    Ollama-style tool calls.

    Groq streams tool calls as index-keyed fragments whose ``arguments``
    string arrives in pieces; Ollama emits complete tool_calls in a single
    chunk. The agent loop expects the Ollama shape, so we accumulate.
    """

    def __init__(self) -> None:
        self._calls: Dict[int, Dict[str, Any]] = {}

    def add(self, fragments: List[dict]) -> None:
        for frag in fragments or []:
            idx = frag.get("index", 0)
            slot = self._calls.setdefault(
                idx,
                {
                    "id": frag.get("id", ""),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if frag.get("id"):
                slot["id"] = frag["id"]
            fn = frag.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]

    def flush(self) -> List[dict]:
        calls = [self._calls[i] for i in sorted(self._calls)]
        self._calls.clear()
        return calls


async def stream_chat(
    model: str,
    messages: List[dict],
    tools: Optional[List[dict]] = None,
    timeout: float = 1200.0,
    **kwargs: Any,
) -> AsyncIterator[Dict[str, Any]]:
    """Streaming chat call routed by provider.

    Yields NORMALIZED chunk dicts so callers handle one format only:

        {"thinking": str,   # reasoning tokens ("" when none)
         "content": str,    # visible tokens ("" when none)
         "tool_calls": [],  # COMPLETE tool calls (ollama shape)
         "done": bool}      # final chunk flag
    """
    if is_groq_model(model):
        payload = _groq_payload(model, messages, stream=True, tools=tools, **kwargs)
        accumulator = _GroqToolCallAccumulator()
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{GROQ_BASE_URL}/chat/completions",
                json=payload,
                headers=_groq_headers(),
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) or {}
                    thinking = delta.get("reasoning", "") or ""
                    content = delta.get("content", "") or ""
                    frags = delta.get("tool_calls")
                    if frags:
                        accumulator.add(frags)
                    finish = choices[0].get("finish_reason")
                    if thinking or content:
                        yield {
                            "thinking": thinking,
                            "content": content,
                            "tool_calls": [],
                            "done": False,
                        }
                    if finish:
                        calls = accumulator.flush()
                        if calls or finish == "tool_calls":
                            yield {
                                "thinking": "",
                                "content": "",
                                "tool_calls": calls,
                                "done": False,
                            }
                        yield {
                            "thinking": "",
                            "content": "",
                            "tool_calls": [],
                            "done": True,
                            "finish_reason": finish,
                        }
                        return
        # Stream ended without finish_reason (connection closed) — flush.
        calls = accumulator.flush()
        if calls:
            yield {"thinking": "", "content": "", "tool_calls": calls, "done": False}
        yield {"thinking": "", "content": "", "tool_calls": [], "done": True}
        return

    # Ollama path — JSON-lines streaming, already close to normalized
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    payload.update(kwargs)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{settings.OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = chunk.get("message", {}) or {}
                yield {
                    "thinking": msg.get("thinking", "") or "",
                    "content": msg.get("content", "") or "",
                    "tool_calls": msg.get("tool_calls", []) or [],
                    "done": bool(chunk.get("done")),
                    "finish_reason": chunk.get("done_reason"),
                }
