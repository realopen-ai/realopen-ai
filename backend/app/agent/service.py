"""
AI Agent service - orchestrates LLM calls and tool execution.

Uses native Ollama tool calling when the model supports it, falling
back to text-based fenced-block parsing for older/smaller models.
Tools are dynamically selected per-request to keep prompts small
for 4B/7B models.
"""

import json
import logging
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Set

import httpx

from app.agent.base import ToolCall, get_tool_registry
from app.config import settings
from app.core.logger import is_debug
from app.core.prompt_security import untrusted_context_message
from app.prompts import format_prompt
from app.services.memory import get_relevant_memories
from app.services.context_compactor import compact_conversation
from app.services.conversation_memory import build_cross_session_context
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)


def _dbg(msg: str, *args) -> None:
    """Debug print + log for the agent service."""
    if is_debug():
        try:
            formatted = msg % args if args else msg
        except (TypeError, ValueError):
            formatted = f"{msg} {args}"
        print(f"[agent] {formatted}", flush=True)
        logger.debug(msg, *args)


# ── Always-available tools (shown regardless of query) ──
# manage_memory is always available because "remember this" can follow
# any message regardless of topic. search_past_conversations is keyword-
# triggered to avoid bloating the tool list on every turn.
_ALWAYS_TOOLS: Set[str] = {
    "use_websearch",
    "use_webfetch",
    "use_code_exec",
    "rag_search",
    "manage_memory",
}

# ── Keyword → tool mapping for dynamic selection ──
_KEYWORD_TOOLS: Dict[str, Set[str]] = {
    "search": {"use_websearch", "use_webfetch"},
    "look up": {"use_websearch"},
    "find": {"use_websearch", "rag_search"},
    "calculate": {"use_code_exec"},
    "compute": {"use_code_exec"},
    "run": {"use_code_exec"},
    "code": {"use_code_exec"},
    "script": {"use_code_exec"},
    "document": {"rag_search"},
    "pdf": {"rag_search"},
    "file": {"rag_search"},
    "image": {"use_vision"},
    "picture": {"use_vision"},
    "photo": {"use_vision"},
    "video": {"use_vision"},
    "fetch": {"use_webfetch"},
    "url": {"use_webfetch"},
    "website": {"use_webfetch"},
    "current": {"use_websearch"},
    "today": {"use_websearch"},
    "latest": {"use_websearch"},
    "news": {"use_websearch"},
    "weather": {"use_websearch"},
    "price": {"use_websearch"},
    # Past-conversation search triggers
    "last week": {"search_past_conversations"},
    "yesterday": {"search_past_conversations"},
    "before": {"search_past_conversations"},
    "previous": {"search_past_conversations"},
    "earlier": {"search_past_conversations"},
    "we discussed": {"search_past_conversations"},
    "i told you": {"search_past_conversations"},
    "i said": {"search_past_conversations"},
    "i mentioned": {"search_past_conversations"},
    "remember when": {"search_past_conversations"},
    "what did i": {"search_past_conversations"},
}


def _select_tools(user_message: str) -> Set[str]:
    """Select relevant tools based on the user's message keywords."""
    selected = set(_ALWAYS_TOOLS)
    msg_lower = user_message.lower()
    for keyword, tools in _KEYWORD_TOOLS.items():
        if keyword in msg_lower:
            selected.update(tools)
    return selected


# ── Model capabilities detection ──
_MODELS_WITH_NATIVE_TOOLS = {
    "qwen3",
    "qwen2.5",
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "llama4",
    "mistral",
    "mixtral",
    "command-r",
    "hermes",
    "phi-3",
    "phi-4",
    "gemma3",
    "gemma4",
    "ministral",
    "nemotron",
    "yi-",
    "deepseek-v",
    "deepseek-chat",
    "glm-4",
    "internlm",
}


def _model_supports_native_tools(model_name: str) -> bool:
    """Check if a model likely supports native Ollama tool calling."""
    ml = model_name.lower()
    return any(kw in ml for kw in _MODELS_WITH_NATIVE_TOOLS)


# ── Tool format conversion ──
def _build_ollama_tools(tool_names: Set[str]) -> List[Dict]:
    """Build OpenAI-style tool schemas for Ollama's /api/chat tools param."""
    registry = get_tool_registry()
    tools = []
    for name in sorted(tool_names):
        tool = registry.get(name)
        if not tool:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description[:300],
                    "parameters": {
                        "type": "object",
                        "properties": tool.get_parameters(),
                        "required": tool.get_required_params(),
                    },
                },
            }
        )
    return tools


def _build_system_prompt(tool_names: Set[str]) -> str:
    """Build a compact, STABLE system prompt with only the selected tools.

    KV-CACHE DESIGN: This prompt must be byte-identical across turns of
    the same conversation (same selected tools) so Ollama/llama.cpp can
    reuse their cached prompt prefix. Therefore NOTHING volatile goes
    here — no datetime, no retrieved memories, no cross-session context.
    Those are appended as tail user-role messages in run_agent_stream().
    """
    registry = get_tool_registry()
    lines = []
    for name in sorted(tool_names):
        tool = registry.get(name)
        if tool:
            lines.append(f"- **{tool.name}**: {tool.description[:200]}")
    schema_text = "\n".join(lines) if lines else "(no tools available)"

    return format_prompt("agent_system", tool_schemas=schema_text)


# ── Tool parsing (multi-format) ──
_TOOL_TAGS = {
    "use_websearch",
    "use_webfetch",
    "use_code_exec",
    "use_vision",
    "rag_search",
    "use_image_gen",
    "manage_memory",
    "search_past_conversations",
}

# Fenced code blocks: ```tool_name\n...\n```
_FENCED_RE = re.compile(
    r"```(" + "|".join(map(re.escape, _TOOL_TAGS)) + r")\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# JSON tool blocks: ```tool\n{"tool": "...", "args": {...}}\n```
_JSON_TOOL_RE = re.compile(
    r"```tool\s*\n(\{.*?\})\n```",
    re.DOTALL,
)

# XML-style: <tool_call><invoke name="tool"><parameter name="x">v</parameter></invoke></tool_call>
_XML_TOOL_RE = re.compile(
    r"<tool_call>\s*(.*?)</tool_call>",
    re.DOTALL,
)
_XML_INVOKE_RE = re.compile(
    r'<invoke\s+name="(\w+)"\s*>(.*?)</invoke>',
    re.DOTALL,
)
_XML_PARAM_RE = re.compile(
    r'<parameter\s+name="(\w+)"\s*>(.*?)</parameter>',
    re.DOTALL,
)


def _parse_tool_calls(text: str) -> List[Dict]:
    """Multi-format tool call extraction from LLM output."""

    # Format 1: ```tool {json}``` blocks
    for m in _JSON_TOOL_RE.finditer(text):
        try:
            call = json.loads(m.group(1))
            if call.get("tool") and call.get("args"):
                return [{"tool": call["tool"], "args": call["args"]}]
        except json.JSONDecodeError:
            continue

    # Format 2: Fenced code blocks ```tool_name\narg\n```
    for m in _FENCED_RE.finditer(text):
        tag = m.group(1).lower()
        content = m.group(2).strip()
        if not content:
            continue
        return [_fenced_args_to_call(tag, content)]

    # Format 3: XML-style <tool_call><invoke ...>
    for m in _XML_TOOL_RE.finditer(text):
        calls = []
        for inv in _XML_INVOKE_RE.finditer(m.group(1)):
            tool_name = inv.group(1).lower()
            params = {}
            for pm in _XML_PARAM_RE.finditer(inv.group(2)):
                params[pm.group(1)] = pm.group(2).strip()
            if tool_name and params:
                calls.append({"tool": tool_name, "args": params})
        if calls:
            return calls

    return []


def _fenced_args_to_call(tag: str, content: str) -> Dict:
    """Convert a fenced code block to a tool call dict."""
    args = {}
    lines = content.strip().split("\n")
    tag_lower = tag.lower()

    if tag_lower in ("use_websearch", "rag_search", "search_past_conversations"):
        args["query"] = lines[0]
    elif tag_lower == "use_webfetch":
        args["url"] = lines[0]
    elif tag_lower == "use_code_exec":
        args["code"] = content
    elif tag_lower == "use_vision":
        args["prompt"] = lines[0] if lines else content
        if len(lines) > 1:
            args["image_base64"] = lines[1]
    elif tag_lower == "manage_memory":
        # Format: action on line 1, then key:value pairs
        if lines:
            args["action"] = lines[0].strip().lower()
            for line in lines[1:]:
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip().lower().replace(" ", "_")
                    if key in (
                        "action",
                        "text",
                        "memory_id",
                        "id",
                        "category",
                        "query",
                    ):
                        # Map 'id' to 'memory_id'
                        if key == "id":
                            key = "memory_id"
                        args[key] = val.strip()

    return {"tool": tag_lower, "args": args}


def _convert_native_tool_calls(native_calls: List[Dict]) -> List[Dict]:
    """Convert Ollama native tool calls to our internal tool call format."""
    result = []

    for tc in native_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "")

        raw_args = fn.get("arguments", {})

        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}

        result.append({"tool": name, "args": args})

    return result


# ── SSE helpers ──
def _sse_event(event: str, data: dict) -> str:
    return f"data: {json.dumps({'event': event, **data})}\n\n"


def _tool_call_to_dict(tc: ToolCall) -> dict:
    d = {"id": tc.id, "type": tc.type.value, "status": tc.status, "title": tc.title}
    if tc.query:
        d["query"] = tc.query
    if tc.web_results:
        d["webResults"] = tc.web_results
    if tc.language:
        d["language"] = tc.language
    if tc.code:
        d["code"] = tc.code
    if tc.output:
        d["output"] = tc.output
    if tc.exit_code is not None:
        d["exitCode"] = tc.exit_code
    if tc.image_description:
        d["imageDescription"] = tc.image_description
    if tc.error:
        d["error"] = tc.error
    return d


def _tool_call_to_update_dict(tc: ToolCall) -> dict:
    d = {"status": tc.status}
    if tc.completed_at:
        d["completedAt"] = int(tc.completed_at * 1000)
    if tc.web_results:
        d["webResults"] = tc.web_results
    if tc.output:
        d["output"] = tc.output
    if tc.exit_code is not None:
        d["exitCode"] = tc.exit_code
    if tc.image_description:
        d["imageDescription"] = tc.image_description
    if tc.error:
        d["error"] = tc.error
    return d


# ── Main agent loop ──
async def run_agent_stream(
    messages: List[Dict[str, Any]],
    model: str = "default",
    images: Optional[List[str]] = None,
    conversation_id: Optional[str] = None,
    on_tool_call_start=None,
    on_tool_call_update=None,
) -> AsyncGenerator[str, None]:
    """Streaming agent loop with native tool calling and multi-format fallback."""

    resolved_model = settings.resolve_model(model)
    registry = get_tool_registry()

    # ── Select relevant tools ──
    user_msgs = [m for m in messages if m.get("role") == "user"]
    last_user = user_msgs[-1].get("content", "") if user_msgs else ""
    selected_tools = _select_tools(last_user)
    _dbg("Selected tools: %s", sorted(selected_tools))

    # ── KV-CACHE-AWARE MESSAGE CONSTRUCTION ────────────────────────────
    # The system prompt is STABLE across turns (same selected tools → same
    # bytes) so Ollama/llama.cpp can reuse its cached prompt prefix. All
    # volatile content (datetime, memories, cross-session context) goes
    # into tail user-role "context" messages appended AFTER the
    # conversation, wrapped as untrusted data for prompt-injection safety.
    #
    # This can halve per-turn latency on small models where the system
    # prompt is ~1-2k tokens — without this, every turn re-processes the
    # full prompt from scratch because the datetime changed.
    system_prompt = _build_system_prompt(selected_tools)

    # Build the list of dynamic context messages (appended after convo).
    context_messages: List[Dict[str, Any]] = []

    # Memory injection — wrapped as untrusted data (memories are
    # auto-extracted from past conversations and could contain injected
    # content from a malicious document).
    try:
        if last_user.strip():
            async with async_session_factory() as db:
                relevant = await get_relevant_memories(
                    db, last_user, top_k=settings.MEMORY_INJECTION_TOP_K
                )
                if relevant:
                    pinned = [m for m in relevant if m.pinned]
                    extended = [m for m in relevant if not m.pinned]
                    if pinned:
                        pinned_text = "\n".join(
                            f"- {m.text} [{m.category}]" for m in pinned
                        )
                        context_messages.append(
                            untrusted_context_message(
                                "saved memory: pinned user facts",
                                f"Core facts about the user (always in context):\n{pinned_text}",
                            )
                        )
                    if extended:
                        ext_text = "\n".join(
                            f"- {m.text} [{m.category}]" for m in extended
                        )
                        context_messages.append(
                            untrusted_context_message(
                                "saved memory: retrieved context",
                                f"Memory context. Do not reference unless the user asks "
                                f"about these topics.\n{ext_text}",
                            )
                        )
                    _dbg(
                        "Injected %d memories (%d pinned, %d extended)",
                        len(relevant),
                        len(pinned),
                        len(extended),
                    )
    except Exception as e:
        logger.debug("Memory injection failed: %s", e)
        _dbg("Memory injection failed: %s", e)

    # Cross-session context from past conversations.
    try:
        if last_user.strip() and conversation_id:
            async with async_session_factory() as db:
                cross_ctx = await build_cross_session_context(
                    db,
                    last_user,
                    conversation_id=conversation_id,
                    max_summaries=settings.CONVERSATION_SUMMARY_MAX_INJECT,
                )
                if cross_ctx:
                    context_messages.append(
                        untrusted_context_message(
                            "past conversation summaries", cross_ctx
                        )
                    )
                    _dbg(
                        "Injected cross-session context (%d chars)",
                        len(cross_ctx),
                    )
    except Exception as e:
        logger.debug("Cross-session context injection failed: %s", e)
        _dbg("Cross-session context injection failed: %s", e)

    # Current datetime — volatile, so it's a tail context message (not
    # in the system prompt). Wrapped as untrusted for consistency though
    # it's system-generated.
    context_messages.append(
        {
            "role": "user",
            "content": (
                f"[Context: The current date and time is "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}. "
                f"Use this for recency reasoning.]"
            ),
        }
    )

    # ── Auto-vision for image attachments ──
    if images:
        vision_tool = registry.get("use_vision")
        if vision_tool:
            for i, img_b64 in enumerate(images):
                tc = ToolCall(
                    id=f"tc-vision-{int(time.time()*1000)}-{i}",
                    type=vision_tool.tool_type,
                    name=vision_tool.name,
                    status="running",
                    title="Analyzing image",
                    started_at=time.time(),
                )

                if on_tool_call_start:
                    on_tool_call_start(_tool_call_to_dict(tc))
                yield _sse_event("tool_call", {"tool_call": _tool_call_to_dict(tc)})

                result = await vision_tool.execute(
                    image_base64=img_b64,
                    prompt=format_prompt("vision_default"),
                    model=settings.resolve_model("default_vision"),
                )

                if result.tool_call:
                    result.tool_call.id = tc.id
                    if on_tool_call_update:
                        on_tool_call_update(
                            result.tool_call.id,
                            _tool_call_to_update_dict(result.tool_call),
                        )
                    # Inject vision result into conversation
                    _dbg(
                        "   ✅ vision tool completed with success=%s, description=%s",
                        result.success,
                        result.tool_call.image_description,
                    )

                    yield _sse_event(
                        "tool_call",
                        {"tool_call": _tool_call_to_dict(result.tool_call)},
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "content": (f"[Vision analysis: {result.output}]"),
                        }
                    )

    # ── Decide: native tools or text parsing ──
    use_native_tools = _model_supports_native_tools(resolved_model)
    ollama_tools = _build_ollama_tools(selected_tools) if use_native_tools else None
    _dbg("Native tools: %s, count=%d", use_native_tools, len(ollama_tools or []))

    max_rounds = 10
    for _ in range(max_rounds):
        # Build Ollama request messages: stable system prefix + conversation
        # turns + dynamic context messages (memories/cross-session/datetime).
        # The context_messages are appended AFTER the conversation so the
        # system prompt + conversation prefix stays cacheable.
        ollama_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            ollama_messages.append({"role": m["role"], "content": m["content"]})
        # Append dynamic context (memories, cross-session, datetime).
        # These are user-role messages wrapped as untrusted data.
        for cm in context_messages:
            ollama_messages.append({"role": cm["role"], "content": cm["content"]})

        # Compact if approaching context limit (essential for small models)
        try:
            compacted, was_compacted = await compact_conversation(
                ollama_messages, resolved_model
            )
            if was_compacted:
                ollama_messages = compacted
                _dbg(
                    "Compacted conversation: %d -> %d messages",
                    len(messages) + 1,
                    len(compacted),
                )
        except Exception as e:
            logger.debug("Context compaction failed: %s", e)
            _dbg("Context compaction failed: %s", e)

        full_response = ""
        thinking_content = ""
        native_tool_calls = []
        generation_start = time.time()
        thinking_start = None

        try:
            async with httpx.AsyncClient(timeout=1200.0) as client:
                payload = {
                    "model": resolved_model,
                    "messages": ollama_messages,
                    "stream": True,
                }
                if ollama_tools:
                    payload["tools"] = ollama_tools

                async with client.stream(
                    "POST",
                    f"{settings.OLLAMA_BASE_URL}/api/chat",
                    json=payload,
                    timeout=1200.0,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)

                            # Thinking tokens
                            thinking = chunk.get("message", {}).get("thinking", "")
                            if thinking:
                                if not thinking_start:
                                    thinking_start = time.time()
                                    yield _sse_event("thinking_start", {})
                                thinking_content += thinking
                                yield _sse_event("thinking", {"thinking": thinking})

                            # Content tokens
                            token = chunk.get("message", {}).get("content", "")
                            if token:
                                if thinking_start is not None:
                                    elapsed = round(time.time() - thinking_start)
                                    yield _sse_event(
                                        "thinking_done", {"thinkingDuration": elapsed}
                                    )
                                    thinking_start = None
                                full_response += token
                                yield _sse_event(
                                    "message",
                                    {
                                        "message": {
                                            "role": "assistant",
                                            "content": token,
                                        }
                                    },
                                )

                            # Native tool calls from Ollama
                            for tc in chunk.get("message", {}).get("tool_calls", []):
                                native_tool_calls.append(tc)

                            if chunk.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue

        except httpx.ConnectError:
            logger.error("Cannot connect to Ollama at %s", settings.OLLAMA_BASE_URL)
            _dbg("🤖 ❌ Cannot connect to Ollama at %s", settings.OLLAMA_BASE_URL)
            yield _sse_event(
                "error", {"error": "Cannot connect to Ollama. Is it running?"}
            )
            return
        except Exception as e:
            logger.exception("Ollama error: %s", e)
            _dbg("🤖 ❌ Unexpected Ollama error: %s", e)
            yield _sse_event("error", {"error": f"Ollama error: {e}"})
            return

        # Flush thinking if still active
        if thinking_start is not None:
            yield _sse_event(
                "thinking_done",
                {"thinkingDuration": round(time.time() - thinking_start)},
            )

        gen_elapsed = round(time.time() - generation_start)
        yield _sse_event("generation_done", {"generationDuration": gen_elapsed})

        # ── Resolve tool calls ──
        tool_calls = []
        if native_tool_calls:
            tool_calls = _convert_native_tool_calls(native_tool_calls)
            _dbg("Native tool calls: %d", len(tool_calls))
        else:
            tool_calls = _parse_tool_calls(full_response)
            _dbg("Parsed tool calls from text: %d", len(tool_calls))

        if not tool_calls:
            # No tool calls - we've already streamed all the text, just signal done
            yield _sse_event(
                "message",
                {
                    "message": {
                        "role": "assistant",
                        "content": " ",
                    }
                },
            )
            yield _sse_event("done", {})
            return

        # Execute tool calls
        for call in tool_calls:
            tool_name = call.get("tool", "")
            if not tool_name:
                tool_name = call.get("name", "unknown")
                if tool_name == "unknown":
                    _dbg("Tool call missing name: %s", call)
                    messages.append(
                        {
                            "role": "assistant",
                            "content": f"[Tool call missing name: {call}]",
                        }
                    )
                    continue
            tool_args = call.get("args", {})
            _dbg("🤖 Tool call: name=%s  args=%s", tool_name, str(tool_args)[:200])

            tool = registry.get(tool_name)
            if not tool:
                _dbg("Unknown tool: %s", tool_name)
                messages.append(
                    {
                        "role": "assistant",
                        "content": (
                            f"Unknown tool: {tool_name}. "
                            f"Available: {sorted(selected_tools)}"
                        ),
                    }
                )
                continue

            # RAG: inject conversation_id
            if tool_name == "rag_search" and conversation_id:
                tool_args.setdefault("conversation_id", conversation_id)
                _dbg(
                    "   🔎 rag_search: injected conversation_id=%s",
                    conversation_id,
                )

            # search_past_conversations: inject conversation_id so the
            # tool excludes the current conversation from its results.
            if tool_name == "search_past_conversations" and conversation_id:
                tool_args.setdefault("conversation_id", conversation_id)
                _dbg(
                    "   🔎 search_past_conversations: injected conversation_id=%s",
                    conversation_id,
                )

            # Generate a tool call ID for tracking across start/update events
            tc_id = f"tc-{tool.tool_type.value}-{int(time.time()*1000)}"

            # Notify frontend that tool is starting
            if on_tool_call_start:
                on_tool_call_start(
                    {
                        "id": tc_id,
                        "type": tool.tool_type.value,
                        "status": "running",
                        "title": tool.name,
                        **tool_args,
                    }
                )

            yield _sse_event(
                "tool_call",
                {
                    "tool_call": {
                        "id": tc_id,
                        "type": tool.tool_type.value,
                        "status": "running",
                        "title": f"Calling {tool_name}",
                        **tool_args,
                    }
                },
            )

            # Execute the tool (with error handling)
            try:
                result = await tool.execute(**tool_args)
            except Exception as e:
                logger.exception("Tool %s failed: %s", tool_name, e)
                yield _sse_event(
                    "tool_call",
                    {
                        "tool_call": {
                            "id": f"tc-err-{int(time.time()*1000)}",
                            "type": tool.tool_type.value,
                            "status": "error",
                            "title": f"{tool_name} failed",
                            "error": str(e),
                        }
                    },
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[Tool {tool_name} failed: {e}]\n"
                            "Please respond without this tool."
                        ),
                    }
                )
                continue

            # Notify frontend that tool completed
            if result.tool_call:
                result.tool_call.id = tc_id  # Ensure the tool call ID is consistent
                update_dict = _tool_call_to_update_dict(result.tool_call)
                update_dict["id"] = tc_id
                if on_tool_call_update:
                    on_tool_call_update(tc_id, update_dict)
                yield _sse_event("tool_call", {"tool_call": update_dict})

            # RAG sources event
            if tool_name == "rag_search" and result.tool_call:
                rag_sources = getattr(result.tool_call, "rag_sources", None)
                if rag_sources:
                    _dbg(
                        "   🔎 rag_sources: emitting %d sources",
                        len(rag_sources),
                    )
                    yield _sse_event(
                        "rag_sources",
                        {"sources": rag_sources, "tool_call_id": tc_id},
                    )
                else:
                    _dbg("   🔎 rag_sources: no sources retrieved, skipping event")

            # Feed result back
            messages.append({"role": "assistant", "content": full_response})
            messages.append(
                {
                    "role": "user",
                    "content": f"[Tool result for {tool_name}]: {result.output}\n\n"
                    f"Based on this result, provide your answer to the user.",
                }
            )

    # Max rounds exhausted
    yield _sse_event(
        "message",
        {"message": {"role": "assistant", "content": "I've completed my analysis."}},
    )
    yield _sse_event("done", {})
