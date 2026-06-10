"""
AI Agent service - orchestrates LLM calls and tool execution.

The agent loop:
1. Build the prompt with system message + tool schemas
2. Send messages to Ollama
3. Stream tokens from Ollama in real-time to the client
4. If the LLM requests a tool call (detected after full response), execute it
5. Feed tool results back and repeat until no more tool calls
6. Stream the final response back to the API layer

IMPORTANT — Streaming strategy:
We stream tokens from Ollama to the client AS they arrive for immediate
feedback. When we detect a potential tool-call block (```tool), we buffer
tokens until we know whether it's a real tool call or a false alarm.
This gives the user real-time streaming for normal text while still
supporting the tool-call agent loop.
"""

import json
import logging
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from app.agent.base import ToolCall, get_tool_registry
from app.config import settings
from app.core.logger import is_debug
from app.prompts import format_prompt

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


TOOL_JSON_PATTERN = re.compile(r"```tool\s*\n(.*?)\n```", re.DOTALL)

# Marker that indicates the start of a potential tool-call block
_TOOL_BLOCK_START = "```tool"


def _build_system_prompt() -> str:
    """Build the system prompt with current tool schemas."""
    registry = get_tool_registry()
    schemas = registry.get_schemas()
    schema_text = "\n".join(f"- **{s['name']}**: {s['description']}" for s in schemas)
    return format_prompt(
        "agent_system",
        tool_schemas=schema_text,
        current_datetime=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _extract_tool_calls(text: str) -> List[Dict]:
    """Extract tool call JSON blocks from LLM output."""
    calls = []
    for match in TOOL_JSON_PATTERN.finditer(text):
        try:
            call = json.loads(match.group(1).strip())
            calls.append(call)
        except json.JSONDecodeError:
            logger.warning("Failed to parse tool call: %s", match.group(1))
    return calls


def _strip_tool_blocks(text: str) -> str:
    """Remove tool JSON blocks from LLM output, leaving only the natural text."""
    return TOOL_JSON_PATTERN.sub("", text).strip()


class _TokenStreamer:
    """Smart buffer that streams tokens immediately but buffers around
    potential tool-call blocks.

    Strategy:
    - Stream tokens to the client as they arrive (immediate feedback)
    - When we detect the start of a ```tool block, stop streaming and buffer
    - Because tokens may split ``` and tool, we first detect ``` and enter
      a "maybe tool block" state, then confirm if it's actually ```tool
    - When the block closes with ```, the buffered content is a tool call —
      we DON'T stream it (it will be handled via tool_call SSE events)
    - Any text after the closing ``` is streamed normally

    In practice, tool-call blocks are rare, so the common case is pure
    pass-through streaming.

    Implementation detail: we track `streamed_len` - the number of chars
    from `full_response` that have already been emitted as SSE message
    events.
    """

    def __init__(self) -> None:
        self.full_response = ""  # Complete accumulated response
        self.streamed_len = 0  # How many chars we've already emitted

        self.in_tool_block = False  # Are we inside a confirmed ```tool block?
        self.tool_block_buf = ""  # Buffer for confirmed tool block

        self.maybe_tool_block = False  # Saw ``` but not yet sure it's ```tool
        self.maybe_buf = ""  # Buffer for potential tool block

    def feed(self, token: str) -> Optional[str]:
        """Feed a token from Ollama. Returns an SSE string to yield, or None."""
        self.full_response += token

        # ------------------------------------------------------------
        # CASE 1 — Currently inside a confirmed ```tool block
        # ------------------------------------------------------------
        if self.in_tool_block:
            self.tool_block_buf += token

            # Check if the tool block has closed (look for ``` after opening)
            close_idx = self.tool_block_buf.find("```", len(_TOOL_BLOCK_START))
            if close_idx != -1:
                # Everything until closing ``` is tool block (do not stream)
                after_close = self.tool_block_buf[close_idx + 3 :]

                self.in_tool_block = False
                self.tool_block_buf = ""

                # Mark tool block as consumed
                self.streamed_len = len(self.full_response)

                # Stream any trailing text after closing ```
                if after_close:
                    return _sse_event(
                        "message",
                        {"message": {"role": "assistant", "content": after_close}},
                    )
            return None

        # ------------------------------------------------------------
        # CASE 2 — We saw ``` and are checking if it's ```tool
        # ------------------------------------------------------------
        if self.maybe_tool_block:
            self.maybe_buf += token

            # Wait until we have enough characters to confirm ```tool
            if len(self.maybe_buf) >= len(_TOOL_BLOCK_START):
                if self.maybe_buf.startswith(_TOOL_BLOCK_START):
                    # Confirmed tool block
                    self.in_tool_block = True
                    self.tool_block_buf = self.maybe_buf

                    self.maybe_tool_block = False
                    self.maybe_buf = ""
                    return None
                else:
                    # False alarm — this was just a normal ``` block
                    out = self.maybe_buf

                    self.maybe_tool_block = False
                    self.maybe_buf = ""

                    self.streamed_len = len(self.full_response)
                    return _sse_event(
                        "message",
                        {"message": {"role": "assistant", "content": out}},
                    )
            return None

        # ------------------------------------------------------------
        # CASE 3 — Normal streaming, look for ``` start
        # ------------------------------------------------------------
        search_region = self.full_response[self.streamed_len :]
        idx = search_region.find("```")

        if idx != -1:
            # Found ``` — potential start of tool block
            idx += self.streamed_len

            # Stream everything BEFORE ```
            before = self.full_response[self.streamed_len : idx]
            self.streamed_len = idx

            # Enter maybe-tool-block state
            self.maybe_tool_block = True
            self.maybe_buf = self.full_response[idx:]

            if before:
                return _sse_event(
                    "message",
                    {"message": {"role": "assistant", "content": before}},
                )
            return None

        # ------------------------------------------------------------
        # CASE 4 — Normal token, stream immediately
        # ------------------------------------------------------------
        self.streamed_len = len(self.full_response)
        return _sse_event(
            "message",
            {"message": {"role": "assistant", "content": token}},
        )

    def flush_remaining(self) -> Optional[str]:
        """Flush any un-streamed text after Ollama completes.

        If a tool block never closed, treat it as normal text.
        """

        if self.in_tool_block:
            # Tool block never closed — stream as normal text
            self.in_tool_block = False
            unstreamed = self.tool_block_buf
            self.tool_block_buf = ""
            self.streamed_len = len(self.full_response)
            if unstreamed:
                return _sse_event(
                    "message",
                    {"message": {"role": "assistant", "content": unstreamed}},
                )
            return None

        if self.maybe_tool_block:
            # We saw ``` but never confirmed ```tool — treat as normal text
            self.maybe_tool_block = False
            unstreamed = self.maybe_buf
            self.maybe_buf = ""
            self.streamed_len = len(self.full_response)
            if unstreamed:
                return _sse_event(
                    "message",
                    {"message": {"role": "assistant", "content": unstreamed}},
                )
            return None

        if self.streamed_len < len(self.full_response):
            remaining = self.full_response[self.streamed_len :]
            self.streamed_len = len(self.full_response)
            if remaining:
                return _sse_event(
                    "message",
                    {"message": {"role": "assistant", "content": remaining}},
                )
        return None

    def get_clean_text(self) -> str:
        """Get the response text with tool blocks stripped."""
        return _strip_tool_blocks(self.full_response)


async def run_agent_stream(
    messages: List[Dict[str, Any]],
    model: str = "default",
    images: Optional[List[str]] = None,
    on_tool_call_start=None,
    on_tool_call_update=None,
) -> AsyncGenerator[str, None]:
    """
    Run the agent loop and yield SSE-formatted data chunks.

    Tokens from Ollama are streamed to the client in real-time for
    immediate feedback. When the full response is received, we check
    for tool-call blocks. If found, we execute the tools and continue
    the loop; if not, we yield a "done" event.

    Args:
        messages: Conversation messages [{role, content}, ...]
        model: Model role/type/id to resolve
        images: Optional list of base64-encoded images
        on_tool_call_start: Callback(tool_call_dict) when a tool starts
        on_tool_call_update: Callback(tool_call_id, updates_dict) when a tool updates
    """
    resolved_model = settings.resolve_model(model)
    _dbg(
        "🤖 run_agent_stream START  model=%s -> resolved=%s  images=%s  messages=%d",
        model,
        resolved_model,
        "yes" if images else "none",
        len(messages),
    )

    system_prompt = _build_system_prompt()

    # If images are provided, auto-invoke the vision tool first
    if images:
        vision_tool = get_tool_registry().get("use_vision")
        _dbg(
            "   received %d images, invoking vision tool: %s",
            len(images),
            "found" if vision_tool else "not found",
        )
        if vision_tool:
            for i, img_b64 in enumerate(images):
                tool_call = ToolCall(
                    id=f"tc-vision-auto-{int(time.time() * 1000)}-{i}",
                    type=vision_tool.tool_type,
                    name=vision_tool.name,
                    status="running",
                    title="Analyzing image",
                    started_at=time.time(),
                )

                if on_tool_call_start:
                    on_tool_call_start(_tool_call_to_dict(tool_call))

                yield _sse_event(
                    "tool_call",
                    {"tool_call": _tool_call_to_dict(tool_call)},
                )

                result = await vision_tool.execute(
                    image_base64=img_b64,
                    prompt=format_prompt("vision_default"),
                    model=settings.resolve_model("default_vision"),
                )

                if result.tool_call:
                    result.tool_call.id = (
                        tool_call.id
                    )  # Ensure consistent ID for frontend mapping

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
                            "role": "assistant",
                            "content": f"[Vision analysis result: {result.output}]",
                        }
                    )

    # Agent loop: up to 5 tool-call rounds
    max_rounds = 5
    for round_num in range(max_rounds):
        # Build Ollama request messages
        ollama_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            ollama_messages.append({"role": m["role"], "content": m["content"]})

        # Stream from Ollama - tokens are streamed to the client immediately
        streamer = _TokenStreamer()
        thinking_content = ""  # Accumulate thinking/reasoning tokens
        thinking_start_time = None  # Track when thinking started
        generation_start_time = time.time()  # Track total generation time
        try:
            _dbg(
                "🤖 Connecting to Ollama at %s/api/chat  model=%s  round=%d",
                settings.OLLAMA_BASE_URL,
                resolved_model,
                round_num + 1,
            )
            async with httpx.AsyncClient(timeout=1200.0) as client:
                async with client.stream(
                    "POST",
                    f"{settings.OLLAMA_BASE_URL}/api/chat",
                    json={
                        "model": resolved_model,
                        "messages": ollama_messages,
                        "stream": True,
                    },
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)

                            # Handle thinking tokens
                            thinking_token = chunk.get("message", {}).get(
                                "thinking", ""
                            )
                            if thinking_token:
                                if not thinking_start_time:
                                    thinking_start_time = time.time()
                                    # Emit thinking_start event so frontend shows "Thinking..."
                                    yield _sse_event("thinking_start", {})
                                thinking_content += thinking_token
                                # Stream thinking tokens to frontend
                                yield _sse_event(
                                    "thinking",
                                    {"thinking": thinking_token},
                                )

                            # Handle content tokens
                            token = chunk.get("message", {}).get("content", "")
                            if token:
                                # If we were thinking and now getting content, thinking is done
                                if thinking_start_time is not None:
                                    thinking_elapsed = round(
                                        time.time() - thinking_start_time
                                    )
                                    yield _sse_event(
                                        "thinking_done",
                                        {"thinkingDuration": thinking_elapsed},
                                    )
                                    thinking_start_time = (
                                        None  # Reset so we don't emit again
                                    )
                                sse = streamer.feed(token)
                                if sse:
                                    yield sse
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
        except httpx.HTTPStatusError as e:
            logger.error(
                "Ollama returned HTTP %s: %s",
                e.response.status_code,
                e.response.text[:500],
            )
            _dbg("🤖 ❌ Ollama HTTP error %s", e.response.status_code)
            yield _sse_event(
                "error",
                {
                    "error": (
                        f"Ollama returned error {e.response.status_code}. "
                        "Check that the model is pulled and Ollama is running."
                    )
                },
            )
            return
        except httpx.TimeoutException:
            logger.error("Ollama request timed out after 20 minutes")
            _dbg("🤖 ❌ Ollama timeout")
            yield _sse_event(
                "error",
                {
                    "error": (
                        "Ollama request timed out. The model may be loading or the request "
                        "is too complex."
                    )
                },
            )
            return
        except Exception as e:
            logger.exception("Unexpected error calling Ollama: %s", e)
            _dbg("🤖 ❌ Unexpected Ollama error: %s", e)
            yield _sse_event("error", {"error": f"Unexpected error: {e}"})
            return

        # Flush any remaining buffered text
        flush = streamer.flush_remaining()
        if flush:
            yield flush

        full_response = streamer.full_response
        _dbg(
            "🤖 Ollama [%s] response received (%d chars): %s",
            resolved_model,
            len(full_response),
            full_response[:200] + "..." if len(full_response) > 200 else full_response,
        )

        # Calculate total generation duration
        generation_elapsed = round(time.time() - generation_start_time)
        # Calculate thinking duration if we were thinking but content came without
        # a thinking_done event (edge case: model only thinks, no content tokens)
        thinking_elapsed = None
        if thinking_start_time is not None:
            thinking_elapsed = round(time.time() - thinking_start_time)
            yield _sse_event(
                "thinking_done",
                {"thinkingDuration": thinking_elapsed},
            )

        # Emit generation_done event with metadata for DB persistence
        yield _sse_event(
            "generation_done",
            {
                "thinkingDuration": (
                    thinking_elapsed if thinking_elapsed is not None else None
                ),
                "generationDuration": generation_elapsed,
            },
        )

        # Check for tool calls in the complete response
        tool_calls = _extract_tool_calls(full_response)
        _dbg("🤖 Tool calls found: %d", len(tool_calls))

        if not tool_calls:
            # No tool calls - we've already streamed all the text, just signal done
            yield _sse_event("done", {})
            return

        # Execute tool calls
        for call in tool_calls:
            tool_name = call.get("tool", "")
            if not tool_name:
                tool_name = call.get("name", "unknown")
            tool_args = call.get("args", {})
            _dbg("🤖 Tool call: name=%s  args=%s", tool_name, str(tool_args)[:200])

            tool = get_tool_registry().get(tool_name)
            if not tool:
                error_msg = f"Unknown tool: {tool_name}"
                logger.warning(error_msg)
                yield _sse_event(
                    "tool_call",
                    {
                        "tool_call": {
                            "type": "error",
                            "status": "error",
                            "title": error_msg,
                        }
                    },
                )
                messages.append({"role": "assistant", "content": error_msg})
                continue

            # Generate a tool call ID for tracking across start/update events
            tc_id = f"tc-{tool.tool_type.value}-{int(time.time() * 1000)}"

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
                logger.exception("Tool execution failed for %s: %s", tool_name, e)
                yield _sse_event(
                    "tool_call",
                    {
                        "tool_call": {
                            "id": f"tc-err-{int(time.time() * 1000)}",
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
                            f"[Tool {tool_name} failed: {e}]\n\n"
                            "Please respond to the user without this tool."
                        ),
                    }
                )
                continue

            # Notify frontend that tool completed
            if result.tool_call:
                result.tool_call.id = tc_id  # Ensure the tool call ID is consistent
                update_dict = _tool_call_to_update_dict(result.tool_call)
                update_dict["id"] = (
                    result.tool_call.id
                )  # Ensure ID is present for frontend mapping
                if on_tool_call_update:
                    on_tool_call_update(result.tool_call.id, update_dict)

                yield _sse_event("tool_call", {"tool_call": update_dict})

            # Feed tool result back into conversation
            messages.append({"role": "assistant", "content": full_response})
            messages.append(
                {
                    "role": "user",
                    "content": f"[Tool result for {tool_name}]: {result.output}\n\n"
                    "Based on this result, please provide your answer to the user.",
                }
            )

        # Continue the loop - the LLM will see the tool results and respond

    # If we've exhausted max rounds, yield what we have
    yield _sse_event(
        "message",
        {
            "message": {
                "role": "assistant",
                "content": "I've completed my analysis. Let me know if you need more details.",
            }
        },
    )
    yield _sse_event("done", {})


def _tool_call_to_dict(tc: ToolCall) -> dict:
    """Convert a ToolCall to a dict for SSE events."""
    d = {
        "id": tc.id,
        "type": tc.type.value,
        "status": tc.status,
        "title": tc.title,
    }
    if tc.query:
        d["query"] = tc.query
    if tc.web_results:
        d["webResults"] = tc.web_results
    if tc.gen_results:
        d["genResults"] = tc.gen_results
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
    """Convert a ToolCall to an update dict for SSE events."""
    d = {"status": tc.status}
    if tc.completed_at:
        d["completedAt"] = int(tc.completed_at * 1000)
    if tc.web_results:
        d["webResults"] = tc.web_results
    if tc.gen_results:
        d["genResults"] = tc.gen_results
    if tc.output:
        d["output"] = tc.output
    if tc.exit_code is not None:
        d["exitCode"] = tc.exit_code
    if tc.image_description:
        d["imageDescription"] = tc.image_description
    if tc.error:
        d["error"] = tc.error
    return d


def _sse_event(event: str, data: dict) -> str:
    """Format an SSE event."""
    return f"data: {json.dumps({'event': event, **data})}\n\n"
