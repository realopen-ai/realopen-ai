"""Opt-in, observational agent traces. No evaluation or training labels."""

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import math
import os
import re
import time
import tempfile
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings
from app.services import coder_settings

try:
    import fcntl
except ImportError:
    fcntl = None

logger = logging.getLogger(__name__)
SCHEMA_VERSION = "1.3"
_model_defaults = {}
current_trace = ContextVar("agent_trace", default=None)
_queue = None
_worker = None
_active = set()
_SECRET_KEY = re.compile(
    r"(?:api.?key|authorization|access.?token|refresh.?token|password|passwd|secret|credential|cookie|authtoken|^token$)",
    re.I,
)
_ASSET_KEY = re.compile(
    r"^(?:images?|image_base64|audio|audio_base64|file_bytes|binary|base64)$", re.I
)


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_value(value, limit, secrets=(), seen=None, depth=0):
    """Redact before truncation; never repr unknown objects or embed binary assets."""
    seen = set() if seen is None else seen
    if depth > 24:
        return {"omitted": "depth_limit"}
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"omitted": "nonfinite_number"}
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\bBearer\s+[\w.\-]+", "Bearer [REDACTED]", value)
        value = re.sub(
            r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value
        )
        value = re.sub(
            r"(?i)((?:api[_-]?key|password|passwd|access[_-]?token|refresh[_-]?token|token|secret|authorization)[\"']?\s*(?:[=:]|\bis\b)\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
            r"\1[REDACTED]",
            value,
        )
        value = re.sub(
            r"data:[^\s,]+;base64,[A-Za-z0-9+/=]+", "[BINARY DATA OMITTED]", value
        )
        value = re.sub(r"\b(?:sk-|gsk_)[A-Za-z0-9_-]{16,}", "[REDACTED]", value)
        return (
            value
            if len(value) <= limit
            else {
                "text": value[:limit],
                "truncated": True,
                "original_chars": len(value),
            }
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"omitted": "binary", "bytes": len(value)}
    if id(value) in seen:
        return {"omitted": "cycle"}
    seen.add(id(value))
    try:
        if isinstance(value, dict):
            return {
                str(key): (
                    "[REDACTED]"
                    if _SECRET_KEY.search(str(key))
                    else (
                        {
                            "omitted": "asset",
                            "items": (
                                len(item)
                                if isinstance(item, (str, list, bytes))
                                else None
                            ),
                        }
                        if _ASSET_KEY.match(str(key))
                        else safe_value(item, limit, secrets, seen, depth + 1)
                    )
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [safe_value(item, limit, secrets, seen, depth + 1) for item in value]
        return {"omitted": "unsupported_type", "type": type(value).__name__}
    finally:
        seen.remove(id(value))


class Trace:
    def __init__(self, request):
        self.started = time.monotonic()
        self.enqueued = False
        try:
            self.owner_task = asyncio.current_task()
        except RuntimeError:
            self.owner_task = None
        self.round_started = None
        self.tool_started = None
        self.round = None
        self.tool = None
        self.generating = False
        self.prompts = {}
        self.secrets = tuple(
            value
            for key, value in os.environ.items()
            if _SECRET_KEY.search(key) and value
        ) + tuple(
            value
            for key, value in settings.model_dump().items()
            if _SECRET_KEY.search(key) and isinstance(value, str) and value
        )
        self.limit = max(1024, settings.AGENT_TRACE_MAX_TEXT_CHARS)
        self.result_limit = max(1, settings.AGENT_TRACE_MAX_TOOL_RESULT_CHARS)
        self.directory = str(Path(settings.AGENT_TRACE_DIR).resolve())
        self.data = {
            "schema_version": SCHEMA_VERSION,
            "capture_policy": {
                "redaction_version": "1.0",
                "max_text_chars": self.limit,
                "max_tool_result_chars": self.result_limit,
                "binary_assets": "omitted",
            },
            "trace_id": str(uuid.uuid4()),
            "agent": "general",
            "parent_trace_id": None,
            "parent_tool_call_id": None,
            "timestamp": timestamp(),
            "conversation_id": request.get("conversation_id"),
            "model": {
                "requested": request.get("model", "default"),
                "profile": settings.HARDWARE_PROFILE,
            },
            "inference": {
                "requested": {
                    "think": request.get("think"),
                    "max_output_tokens": request.get("max_output_tokens"),
                    "temperature": None,
                },
                "think": "provider_default",
                "max_output_tokens": "provider_default",
                "temperature": "provider_default",
            },
            "max_rounds": settings.AGENT_MAX_ROUNDS,
            "request": {
                "messages": self.safe(request.get("messages", [])),
                "user_message": self.safe(
                    request.get("trace_user_message")
                    if request.get("trace_user_message") is not None
                    else next(
                        (
                            m.get("content", "")
                            for m in reversed(request.get("messages", []))
                            if m.get("role") == "user"
                        ),
                        "",
                    )
                ),
                "image_count": len(request.get("images") or []),
                "interaction_mode": request.get("interaction_mode", "text"),
            },
            "rounds": [],
            "preparation_tools": [],
            "outcome": {"status": "running"},
        }

    def safe(self, value, result=False):
        sanitized = safe_value(
            value, self.result_limit if result else self.limit, self.secrets
        )
        if result and not isinstance(sanitized, (str, type(None))):
            serialized = json.dumps(sanitized, ensure_ascii=False)
            if len(serialized) > self.result_limit:
                return {
                    "preview_json": serialized[: self.result_limit],
                    "truncated": True,
                    "original_chars": len(serialized),
                }
        return sanitized

    def resolved_model(self, model):
        self.data["model"]["resolved"] = model

    def limits(self, max_rounds):
        self.data["max_rounds"] = max_rounds

    def messages(self, messages):
        result = []
        for message in messages:
            if message.get("role") == "system":
                content = self.safe(message.get("content", ""))
                digest = hashlib.sha256(
                    json.dumps(content, sort_keys=True).encode()
                ).hexdigest()
                self.prompts[digest] = content
                result.append(
                    {
                        "role": "system",
                        "prompt_ref": f"prompts/{digest}.json",
                        "sha256": digest,
                    }
                )
            else:
                content = message.get("content", "")
                is_result = (
                    message.get("role") == "tool"
                    or isinstance(content, str)
                    and content.startswith("[Tool result for ")
                )
                if is_result:
                    result.append(
                        {
                            **self.safe(
                                {
                                    key: value
                                    for key, value in message.items()
                                    if key != "content"
                                }
                            ),
                            "content": self.safe(content, result=True),
                        }
                    )
                else:
                    result.append(self.safe(message))
        return result

    def end_round(self, decision):
        if self.round is not None:
            self.round["duration_ms"] = round(
                (time.monotonic() - self.round_started) * 1000, 3
            )
            self.round["decision"] = decision
        self.generating = False

    def begin_round(self, number, model, messages, tools, names, context, inference):
        self.end_round("continued")
        self.round_started = time.monotonic()
        self.round = {
            "round": number,
            "timestamp": timestamp(),
            "model": model,
            "input_messages": self.messages(messages),
            "prefix": {
                "system_sha256": hashlib.sha256(
                    str(
                        messages[0].get("content", "")
                        if messages and messages[0].get("role") == "system"
                        else ""
                    ).encode()
                ).hexdigest(),
                "tools_sha256": hashlib.sha256(
                    json.dumps(tools, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
                "note": "Application hashes, not runtime KV-cache keys. Native tool templates, protocol/schema changes and compaction may invalidate cache reuse.",
            },
            "context_messages": self.safe(context),
            "exposed_tool_schemas": self.safe(tools),
            "available_tools": (
                sorted(schema.get("function", {}).get("name", "") for schema in tools)
                if tools is not None
                else sorted(names)
            ),
            "selected_tools": sorted(names),
            "inference": {
                **self.safe(inference),
                "requested": self.safe(inference),
                "think": "provider_default",
                "temperature": "provider_default",
                "max_output_tokens": "provider_default",
                "thinking_observed": False,
            },
            "model_response": {"content": "", "thinking": "", "native_tool_calls": []},
            "tool_calls": [],
            "parse_diagnostics": [],
            "decision": "running",
        }
        self.data["rounds"].append(self.round)
        if number == 1:
            self.data["inference"] = self.round["inference"]
        self.data["model"].setdefault("resolved", model)
        self.generating = True

    def chunk(self, chunk):
        if self.round is None:
            return
        response = self.round["model_response"]
        if chunk.get("thinking"):
            self.round["inference"]["thinking_observed"] = True
        for key in ("content", "thinking"):
            response[key] += (chunk.get(key) or "")[
                : max(0, self.limit - len(response[key]))
            ]
            if len(response[key]) >= self.limit:
                response["truncated"] = True
        response["native_tool_calls"].extend(self.safe(chunk.get("tool_calls") or []))
        if chunk.get("finish_reason"):
            response["finish_reason"] = chunk["finish_reason"]
        if chunk.get("usage"):
            self.round["runtime_usage"] = self.safe(chunk["usage"])

    def model_retry(self):
        # Failed raw output remains in model_attempts. The round response must
        # describe the selected attempt, not concatenate discarded calls.
        if self.round is not None:
            self.round["model_response"] = {
                "content": "",
                "thinking": "",
                "native_tool_calls": [],
            }

    def result_view(self, result_ref, output, shortened):
        if self.round and self.round["tool_calls"]:
            self.round["tool_calls"][-1]["model_view"] = {
                "result_ref": result_ref,
                **({"output": self.safe(output, result=True)} if shortened else {}),
                "shortened": shortened,
            }

    def parsed(self, calls, native, text):
        self.generating = False
        diagnostics = self.round["parse_diagnostics"]
        for call in native:
            args = (call.get("function") or {}).get("arguments", {})
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                    diagnostics.append(
                        {
                            "name": (call.get("function") or {}).get("name"),
                            "status": (
                                "parsed"
                                if isinstance(parsed, dict)
                                else "invalid_shape"
                            ),
                        }
                    )
                except ValueError as exc:
                    diagnostics.append(
                        {
                            "status": "invalid_json",
                            "error": str(exc),
                            "raw_arguments": self.safe(args),
                        }
                    )
            else:
                diagnostics.append(
                    {
                        "name": (call.get("function") or {}).get("name"),
                        "status": (
                            "parsed" if isinstance(args, dict) else "invalid_shape"
                        ),
                    }
                )
        # Observe rejected text candidates without changing the existing parser.
        for match in re.finditer(r"```tool\b\s*\n?(.*?)```", text, re.S):
            try:
                json.loads(match[1])
            except ValueError as exc:
                diagnostics.append(
                    {
                        "status": "invalid_json",
                        "error": str(exc),
                        "raw_candidate": self.safe(match[1]),
                        "represented_by_call": bool(
                            len(calls) == 1 and calls[0].get("parse_error")
                        ),
                    }
                )
        self.round["parsed_tool_calls"] = self.safe(calls)

    def provider_request(self, model, payload, defaults=None):
        if (
            asyncio.current_task() is self.owner_task
            and self.generating
            and self.round
            and model == self.round["model"]
        ):
            self.round["provider_settings"] = self.safe(
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"messages", "tools"}
                }
            )
            defaults = defaults or {}
            self.round["model_defaults"] = self.safe(defaults)
            parameters = defaults.get("parameters", {})
            options = payload.get("options") or {}
            sources = {}
            for field, key in (
                ("temperature", "temperature"),
                ("max_output_tokens", "num_predict"),
                ("think", "think"),
            ):
                value = (
                    payload.get("max_tokens")
                    if field == "max_output_tokens"
                    else payload.get(key)
                )
                if value is None:
                    value = options.get(key)
                if value is not None:
                    source = "request_payload"
                    if self.round["inference"]["requested"].get(field) is None:
                        self.round["inference"]["requested"][field] = value
                elif parameters.get(key) is not None:
                    value, source = parameters[key], "model_metadata"
                else:
                    value, source = "provider_default", "not_exposed"
                if field == "think" and defaults.get("provider") == "groq":
                    value, source = "provider_managed", "not_forwarded_by_adapter"
                self.round["inference"][field] = value
                sources[field] = source
            self.round["inference"]["sources"] = sources

    def malformed_frame(self, model, raw):
        if (
            asyncio.current_task() is self.owner_task
            and self.generating
            and self.round
            and model == self.round["model"]
        ):
            self.round.setdefault("malformed_provider_frames", []).append(
                self.safe(raw)
            )

    def model_attempt(
        self,
        messages,
        tools,
        response=None,
        error=None,
        recovery=False,
        started_at=None,
        duration_ms=None,
    ):
        if self.round is not None:
            self.round["inference"]["native_tools"] = bool(tools)
            if isinstance(response, dict) and (response.get("message") or {}).get(
                "thinking"
            ):
                self.round["inference"]["thinking_observed"] = True
            self.round.setdefault("model_attempts", []).append(
                {
                    "timestamp": started_at or timestamp(),
                    "duration_ms": duration_ms,
                    "input_messages": self.messages(messages),
                    "exposed_tool_schemas": self.safe(tools),
                    "recovery": recovery,
                    "response": self.safe(response),
                    "error": self.safe(error),
                    "inference": self.safe(self.round["inference"]),
                    "provider_settings": self.safe(self.round.get("provider_settings")),
                    "model_defaults": self.safe(self.round.get("model_defaults")),
                }
            )

            if isinstance(response, dict) and response.get("usage"):
                self.round["runtime_usage"] = self.safe(response["usage"])

    def coder_identity(self, task_id, sandbox_id):
        self.data["coder_task_id"] = str(task_id)
        self.data["sandbox_id"] = str(sandbox_id)

    def recovery_error(self, error):
        if self.round is not None:
            self.round["parse_diagnostics"].append(
                {
                    "status": "invalid_recovery_response",
                    "error": self.safe(error),
                }
            )

    def begin_tool(self, name, args, origin=None):
        self.tool_started = time.monotonic()
        self.tool = {
            "name": name,
            "arguments": self.safe(args),
            "origin": origin or ("model" if self.round else "application"),
            "timestamp": timestamp(),
            "execution_status": "pending",
            "validation": {"status": "not_observed"},
        }
        (
            self.round["tool_calls"] if self.round else self.data["preparation_tools"]
        ).append(self.tool)

    def tool_identity(self, tool_call_id):
        if self.tool is not None:
            self.tool["tool_call_id"] = tool_call_id

    def end_tool(
        self,
        status,
        output=None,
        error=None,
        execution_args=None,
        validation_error=False,
    ):
        if self.tool is None:
            return
        self.tool.update(
            execution_status=status,
            output=output,
            error=self.safe(error),
            duration_ms=round((time.monotonic() - self.tool_started) * 1000, 3),
        )
        if execution_args is not None:
            self.tool["effective_arguments"] = self.safe(
                {k: v for k, v in execution_args.items() if not k.startswith("_")}
            )
        # Validation is owned by tools, not duplicated by the observer.
        self.tool["validation"] = {
            "status": (
                "tool_returned_success"
                if status == "completed"
                else (
                    "reported_validation_error"
                    if validation_error
                    or (
                        isinstance(error, str)
                        and (error.startswith("Invalid ") or "ValidationError" in error)
                    )
                    else "not_observed"
                )
            ),
            "error": self.safe(error),
        }
        self.tool = None

    def finish(self, status, reason=None, response=None):
        if self.data["outcome"]["status"] != "running":
            return
        if self.tool:
            self.end_tool(
                (
                    "interrupted"
                    if status == "shutdown"
                    else "cancelled" if status == "cancelled" else "failed"
                ),
                error=reason,
            )
        self.end_round(status)
        calls = self.data["preparation_tools"] + [
            call for rnd in self.data["rounds"] for call in rnd["tool_calls"]
        ]
        self.data["outcome"] = {
            "status": status,
            "termination_reason": reason,
            "total_rounds": len(self.data["rounds"]),
            "total_tool_calls": len(calls)
            + sum(
                sum(
                    d.get("status") == "invalid_json"
                    and "raw_candidate" in d
                    and not d.get("represented_by_call")
                    for d in rnd["parse_diagnostics"]
                )
                for rnd in self.data["rounds"]
            ),
            "unparsed_tool_attempts": sum(
                sum(
                    d.get("status") == "invalid_json" and "raw_candidate" in d
                    for d in rnd["parse_diagnostics"]
                )
                for rnd in self.data["rounds"]
            ),
            "successful_tool_calls": sum(
                c["execution_status"] == "completed" for c in calls
            ),
            "failed_tool_calls": sum(
                c["execution_status"] in {"failed", "unavailable"} for c in calls
            )
            + sum(
                sum(
                    d.get("status") == "invalid_json"
                    and "raw_candidate" in d
                    and not d.get("represented_by_call")
                    for d in rnd["parse_diagnostics"]
                )
                for rnd in self.data["rounds"]
            ),
            "duration_ms": round((time.monotonic() - self.started) * 1000, 3),
            "interrupted_tool_calls": sum(
                c["execution_status"] in {"cancelled", "interrupted"} for c in calls
            ),
            "skipped_tool_calls": sum(
                c["execution_status"] == "skipped" for c in calls
            ),
            "final_assistant_response": (
                response
                if response is not None
                else (
                    self.data["rounds"][-1]["model_response"]["content"]
                    if self.data["rounds"]
                    else ""
                )
            ),
            "final_response_source": (
                "application"
                if response is not None
                else "model" if self.data["rounds"] else None
            ),
            "quality_evaluated": False,
        }


def observe(method, *args, **kwargs):
    trace = current_trace.get()
    if trace is not None and (
        trace.data["outcome"]["status"] == "running" or method == "finish"
    ):
        try:
            getattr(trace, method)(*args, **kwargs)
        except Exception:
            logger.warning("Agent trace observation failed", exc_info=False)


async def observe_provider_request(model, payload, local=False):
    """Read model defaults only for traced requests; never alter inference payloads."""
    trace = current_trace.get()
    if (
        trace is None
        or asyncio.current_task() is not trace.owner_task
        or not trace.generating
    ):
        return
    defaults = {
        "lookup_status": "not_exposed",
        "provider": "groq" if not local else "ollama",
    }
    try:
        if local:
            key = (settings.OLLAMA_BASE_URL, model)
            cached = _model_defaults.get(key)
            if cached and time.monotonic() - cached[0] < (
                300 if cached[1].get("lookup_status") == "available" else 30
            ):
                defaults = cached[1]
            else:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    response = await client.post(
                        f"{settings.OLLAMA_BASE_URL}/api/show", json={"model": model}
                    )
                    response.raise_for_status()
                    metadata = response.json()
                parameters = metadata.get("parameters", {})
                if isinstance(parameters, str):
                    parsed = {}
                    for line in parameters.splitlines():
                        parts = line.split(None, 1)
                        if len(parts) == 2:
                            try:
                                parsed[parts[0]] = json.loads(parts[1])
                            except ValueError:
                                parsed[parts[0]] = parts[1]
                    parameters = parsed
                defaults = {
                    "lookup_status": "available",
                    "provider": "ollama",
                    "parameters": parameters if isinstance(parameters, dict) else {},
                    "model_modified_at": metadata.get("modified_at"),
                    "fetched_at": timestamp(),
                }
                if len(_model_defaults) >= 128:
                    _model_defaults.clear()
                _model_defaults[key] = (time.monotonic(), defaults)
    except Exception as exc:
        defaults = {
            "lookup_status": "unavailable",
            "provider": "ollama" if local else "groq",
            "error_type": type(exc).__name__,
        }
        if local:
            if len(_model_defaults) >= 128:
                _model_defaults.clear()
            _model_defaults[(settings.OLLAMA_BASE_URL, model)] = (
                time.monotonic(),
                defaults,
            )
    observe("provider_request", model, payload, defaults)


def _write(directory, data, prompts):
    # flock protects concurrent app workers; all disk I/O happens off the event loop.
    if fcntl is None:
        raise OSError("Trace persistence requires POSIX file locking")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    assets = root / "prompts"
    assets.mkdir(exist_ok=True, mode=0o700)
    lock_fd = os.open(root / ".write.lock", os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            for digest, prompt in prompts.items():
                path = assets / f"{digest}.json"
                if path.exists():
                    continue
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", dir=assets, delete=False
                    ) as stream:
                        temporary = Path(stream.name)
                        json.dump(
                            {"schema_version": SCHEMA_VERSION, "content": prompt},
                            stream,
                            ensure_ascii=True,
                        )
                    os.replace(temporary, path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            path = root / f"{data['timestamp'][:10]}.jsonl"
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(data, ensure_ascii=True, allow_nan=False) + "\n"
                )
                stream.flush()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _persist(trace):
    # Bound/redact large results off the event loop, preserving raw data only in memory.
    def calls(items):
        return [
            {**call, "output": trace.safe(call.get("output"), result=True)}
            for call in items
        ]

    record = {
        **trace.data,
        "preparation_tools": calls(trace.data["preparation_tools"]),
        "rounds": [
            {**round_, "tool_calls": calls(round_["tool_calls"])}
            for round_ in trace.data["rounds"]
        ],
    }
    _write(trace.directory, trace.safe(record), trace.safe(trace.prompts))


async def _consume(queue):
    while True:
        trace = await queue.get()
        try:
            # Sanitize joined token text too, so chunk boundaries cannot bypass redaction.
            await asyncio.to_thread(_persist, trace)
        except Exception:
            logger.warning("Agent trace persistence failed", exc_info=False)
        finally:
            queue.task_done()


def enqueue(trace):
    global _queue, _worker
    if trace.enqueued:
        return
    trace.enqueued = True
    try:
        if _queue is None or _worker is None or _worker.done():
            _queue = asyncio.Queue(maxsize=max(1, settings.AGENT_TRACE_QUEUE_SIZE))
            _worker = asyncio.create_task(_consume(_queue))
        _queue.put_nowait(trace)
    except Exception:
        logger.warning(
            "Agent trace queue full or unavailable; trace dropped", exc_info=False
        )


async def flush_traces():
    global _queue, _worker
    # Background-owned generations can outlive HTTP requests during shutdown.
    # Snapshot them without changing/cancelling their agent behavior.
    for trace in tuple(_active):
        try:
            trace.finish("shutdown", "application shutdown; partial execution snapshot")
            enqueue(trace)
        except Exception:
            logger.warning("Agent trace shutdown snapshot failed", exc_info=False)
    if _queue is not None:
        await _queue.join()
    if _worker is not None:
        _worker.cancel()
        await asyncio.gather(_worker, return_exceptions=True)
    _queue = _worker = None


def traced_execution(function):
    """Finalize partial traces on task cancellation and async-generator closure."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        if not settings.AGENT_TRACE_ENABLED:
            stream = function(*args, **kwargs)
            try:
                async for item in stream:
                    yield item
            finally:
                await stream.aclose()
            return
        trace = None
        token = None
        stream = function(*args, **kwargs)
        try:
            try:
                trace = Trace(signature.bind(*args, **kwargs).arguments)
                token = current_trace.set(trace)
                _active.add(trace)
            except Exception:
                logger.warning("Agent trace initialization failed", exc_info=False)
            async for item in stream:
                yield item
            observe("finish", "completed", "agent stream ended")
        except (asyncio.CancelledError, GeneratorExit):
            observe("finish", "cancelled", "task cancelled or stream closed")
            raise
        except BaseException as exc:
            observe("finish", "failed", type(exc).__name__ + ": " + str(exc))
            raise
        finally:
            try:
                await stream.aclose()
            finally:
                if token is not None:
                    try:
                        current_trace.reset(token)
                    except ValueError:
                        # Async generator finalizers can run in a copied context.
                        current_trace.set(None)
                if trace is not None:
                    _active.discard(trace)
                    enqueue(trace)

    return wrapper


def traced_coder_execution(function):
    """Give each delegation its own trace/context without changing tool behavior."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        if not settings.AGENT_TRACE_ENABLED:
            return await function(*args, **kwargs)
        trace = token = None
        try:
            try:
                request = signature.bind(*args, **kwargs).arguments
                parent = current_trace.get()
                trace = Trace(
                    {
                        "conversation_id": request.get("conversation_id"),
                        "messages": [
                            {"role": "user", "content": request.get("task", "")}
                        ],
                        "model": "coder",
                    }
                )
                trace.directory = str(Path(trace.directory) / "coder")
                trace.data.update(
                    agent="coder",
                    parent_trace_id=parent.data["trace_id"] if parent else None,
                    parent_tool_call_id=request.get("_parent_tool_call_id"),
                    max_rounds=coder_settings.effective().max_rounds,
                )
                token = current_trace.set(trace)
                _active.add(trace)
            except Exception:
                logger.warning("Coder trace initialization failed", exc_info=False)
                token = current_trace.set(None)
            result = await function(*args, **kwargs)
            observe(
                "finish",
                "completed" if result.success else "failed",
                "coder tool returned",
            )
            return result
        except asyncio.CancelledError:
            observe("finish", "cancelled", "coder task cancelled")
            raise
        except BaseException as exc:
            observe("finish", "failed", type(exc).__name__ + ": " + str(exc))
            raise
        finally:
            if token is not None:
                current_trace.reset(token)
            if trace is not None:
                _active.discard(trace)
                enqueue(trace)

    return wrapper
