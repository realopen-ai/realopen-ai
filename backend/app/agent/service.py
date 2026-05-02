"""
AI Agent service - orchestrates LLM calls and tool execution.

The agent loop:
1. Build the prompt with system message + tool schemas
2. Send messages to Ollama
3. If the LLM requests a tool call, execute it and feed results back
4. Repeat until the LLM produces a final answer (no more tool calls)
5. Stream the final response back to the API layer
"""

import json
import logging
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from app.agent.base import ToolCall, get_tool_registry
from app.config import settings

logger = logging.getLogger(__name__)

# System prompt that teaches the LLM how to use tools
AGENT_SYSTEM_PROMPT = """You are RealOpen-AI, a helpful AI assistant running entirely offline on the user's hardware.

You have access to the following tools. When you need to use a tool, respond with a JSON block in this exact format:

```tool
{"tool": "tool_name", "args": {"arg1": "value1", ...}}
```

Available tools:
{tool_schemas}

IMPORTANT RULES:
- Only use tools when they are genuinely helpful for answering the user's question.
- After receiving tool results, synthesize them into a natural, helpful response.
- If you don't need any tools, just respond normally.
- For web searches, use "use_websearch" with a "query" argument.
- For image analysis, use "use_vision" with "image_base64" and optional "prompt" arguments.
- For code execution, use "use_code_exec" with "code" and optional "language" arguments.
- You may use multiple tools in a single response if needed.
- Never invent or fabricate tool results.
- If a tool fails, explain the error to the user and suggest alternatives.
"""


TOOL_JSON_PATTERN = re.compile(r"```tool\s*\n(.*?)\n```", re.DOTALL)


def _build_system_prompt() -> str:
    """Build the system prompt with current tool schemas."""
    registry = get_tool_registry()
    schemas = registry.get_schemas()
    schema_text = "\n".join(f"- **{s['name']}**: {s['description']}" for s in schemas)
    return AGENT_SYSTEM_PROMPT.format(tool_schemas=schema_text)


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


async def run_agent_stream(
    messages: List[Dict[str, Any]],
    model: str = "default",
    images: Optional[List[str]] = None,
    on_tool_call_start=None,
    on_tool_call_update=None,
) -> AsyncGenerator[str, None]:
    """
    Run the agent loop and yield SSE-formatted data chunks.

    Args:
        messages: Conversation messages [{role, content}, ...]
        model: Model role/type/id to resolve
        images: Optional list of base64-encoded images
        on_tool_call_start: Callback(tool_call_dict) when a tool starts
        on_tool_call_update: Callback(tool_call_id, updates_dict) when a tool updates
    """
    resolved_model = settings.resolve_model(model)
    system_prompt = _build_system_prompt()

    # If images are provided, auto-invoke the vision tool first
    if images:
        vision_tool = get_tool_registry().get("use_vision")
        logger.info(
            "Received %d images, invoking vision tool: %s",
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

                result = await vision_tool.execute(
                    image_base64=img_b64,
                    prompt="Describe this image in detail. What do you see?",
                    model=settings.resolve_model("default_vision"),
                )

                if result.tool_call:
                    if on_tool_call_update:
                        on_tool_call_update(
                            result.tool_call.id,
                            _tool_call_to_update_dict(result.tool_call),
                        )
                    # Inject vision result into conversation
                    logger.info(
                        "Vision tool completed with success=%s, description=%s",
                        result.success,
                        result.tool_call.image_description,
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": f"[Vision analysis result: {result.output}]",
                        }
                    )

    # Agent loop: up to 5 tool-call rounds
    max_rounds = 5
    for _ in range(max_rounds):
        # Build Ollama request messages
        ollama_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            ollama_messages.append({"role": m["role"], "content": m["content"]})

        # Stream from Ollama
        full_response = ""
        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
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
                            token = chunk.get("message", {}).get("content", "")
                            if token:
                                full_response += token
                                # Check if this looks like a tool call being formed
                                # If so, we buffer until the block is complete
                                # For now, just yield everything and process after
                                pass
                            if chunk.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
        except httpx.ConnectError:
            logger.error("Cannot connect to Ollama at %s", settings.OLLAMA_BASE_URL)
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
            yield _sse_event(
                "error",
                {
                    "error": f"Ollama returned error {e.response.status_code}. Check that the model is pulled and Ollama is running."
                },
            )
            return
        except httpx.TimeoutException:
            logger.error("Ollama request timed out after 600s")
            yield _sse_event(
                "error",
                {
                    "error": "Ollama request timed out. The model may be loading or the request is too complex."
                },
            )
            return
        except Exception as e:
            logger.exception("Unexpected error calling Ollama: %s", e)
            yield _sse_event("error", {"error": f"Unexpected error: {e}"})
            return

        logger.info(
            "LLM response received: %s",
            full_response[:200] + "..." if len(full_response) > 200 else full_response,
        )

        # Check for tool calls in the complete response
        tool_calls = _extract_tool_calls(full_response)

        if not tool_calls:
            # No tool calls - stream the final response token by token
            clean_text = _strip_tool_blocks(full_response)
            if clean_text:
                # Re-stream the clean text word by word
                words = clean_text.split(" ")
                for i, word in enumerate(words):
                    token = word if i == 0 else f" {word}"
                    yield _sse_event(
                        "message", {"message": {"role": "assistant", "content": token}}
                    )
            yield _sse_event("done", {})
            return

        # Execute tool calls
        text_before_tools = _strip_tool_blocks(full_response)
        if text_before_tools:
            yield _sse_event(
                "message",
                {"message": {"role": "assistant", "content": text_before_tools}},
            )

        for call in tool_calls:
            tool_name = call.get("tool", "")
            tool_args = call.get("args", {})

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
                        "content": f"[Tool {tool_name} failed: {e}]\n\nPlease respond to the user without this tool.",
                    }
                )
                continue

            # Notify frontend that tool completed
            if result.tool_call:
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
                    "content": f"[Tool result for {tool_name}]: {result.output}\n\nBased on this result, please provide your answer to the user.",
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
    if tc.results:
        d["results"] = tc.results
    if tc.language:
        d["language"] = tc.language
    if tc.code:
        d["code"] = tc.code
    if tc.output:
        d["output"] = tc.output
    if tc.exit_code is not None:
        d["exitCode"] = tc.exit_code
    if tc.image_description:
        d["image_description"] = tc.image_description
    if tc.error:
        d["error"] = tc.error
    return d


def _tool_call_to_update_dict(tc: ToolCall) -> dict:
    """Convert a ToolCall to an update dict for SSE events."""
    d = {"status": tc.status}
    if tc.completed_at:
        d["completedAt"] = int(tc.completed_at * 1000)
    if tc.results:
        d["results"] = tc.results
    if tc.output:
        d["output"] = tc.output
    if tc.exit_code is not None:
        d["exitCode"] = tc.exit_code
    if tc.image_description:
        d["image_description"] = tc.image_description
    if tc.error:
        d["error"] = tc.error
    return d


def _sse_event(event: str, data: dict) -> str:
    """Format an SSE event."""
    return f"data: {json.dumps({'event': event, **data})}\n\n"
