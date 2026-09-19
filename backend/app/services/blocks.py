"""Ordered block reconstruction from agent SSE events.

Shared by the text-chat streaming endpoints (``/api/chat/stream`` and
``/api/chat/stream/multipart``) and the voice WebSocket session
(``app/voice/session.py``) — both consume the SAME agent loop
(``run_agent_stream``) and must persist identical ``blocks`` structures.

Moved verbatim from ``app/api/chat.py`` (where it lived as the private
``_BlockBuilder``) so voice can reuse it without duplicating logic.

Block types:
  - thinking:  {type, content, duration}
  - text:      {type, content}
  - tool_call: {type, tool_call: {id, type, status, title, ...}}
  - error:     {type, content}
"""

import time


class BlockBuilder:
    """Reconstructs the ordered `blocks` array from SSE events.

    Both the /chat/stream and /chat/stream/multipart generate() functions
    (and the voice session's agent-turn task) use this to accumulate
    blocks in chronological order as the agent emits events. The
    resulting blocks list is persisted to the DB and also matches what
    the frontend reconstructs independently.

    Block types:
      - thinking: {type, content, duration}
      - text:     {type, content}
      - tool_call: {type, tool_call: {id, type, status, title, ...}}
      - error:    {type, content}
    """

    def __init__(self):
        self.blocks: list[dict] = []
        self._current_text: dict | None = None
        self._current_thinking: dict | None = None
        self._tool_call_blocks: dict[str, dict] = {}  # tc_id -> block ref
        self.generation_duration: int = 0
        self.deliverables: list[dict] = []  # report/file deliverables for DB

    def _close_text(self):
        self._current_text = None

    def _close_thinking(self):
        self._current_thinking = None

    def on_thinking_start(self):
        """Open a new thinking block (closes any open text block)."""
        self._close_text()
        self._current_thinking = {"type": "thinking", "content": "", "duration": None}
        self.blocks.append(self._current_thinking)

    def on_thinking_token(self, token: str):
        if self._current_thinking is not None:
            self._current_thinking["content"] += token

    def on_thinking_done(self, duration: int):
        if self._current_thinking is not None:
            self._current_thinking["duration"] = duration
        self._close_thinking()

    def on_message_token(self, token: str):
        """Append a text token. Opens a new text block if needed."""
        if self._current_thinking is not None:
            # thinking_done should have fired, but just in case
            self._close_thinking()
        if self._current_text is None:
            self._current_text = {"type": "text", "content": ""}
            self.blocks.append(self._current_text)
        self._current_text["content"] += token

    def on_tool_call_start(self, tc: dict):
        """Create a new tool_call block (closes any open text/thinking)."""
        self._close_text()
        self._close_thinking()
        tc_id = tc.get("id", "")
        block = {"type": "tool_call", "tool_call": dict(tc)}
        self.blocks.append(block)
        if tc_id:
            self._tool_call_blocks[tc_id] = block

    def on_tool_call_update(self, tc_id: str, updates: dict):
        """Update an existing tool_call block by ID."""
        block = self._tool_call_blocks.get(tc_id)
        if block is not None:
            block["tool_call"].update(updates)
        # Extract deliverables from genResults (reports, presentations,
        # excel workbooks, etc.)
        gen_results = updates.get("genResults")
        if isinstance(gen_results, list):
            for gr in gen_results:
                if isinstance(gr, dict) and gr.get("type") in (
                    "report",
                    "presentation",
                    "excel",
                ):
                    # Add to the deliverables list for DB persistence
                    self.deliverables.append(
                        {
                            "type": gr.get("type", "report"),
                            "format": gr.get("format", "pdf"),
                            "filename": gr.get("filename", "report"),
                            "file_path": gr.get("file_path", ""),
                            "download_url": gr.get("download_url", ""),
                            "thumbnail_url": gr.get("thumbnail_url"),
                            "report_id": gr.get("report_id", ""),
                            "created_at": gr.get("created_at", int(time.time())),
                        }
                    )

    def on_rag_sources(self, tc_id: str, sources: list):
        """Attach RAG sources to a tool_call block by ID."""
        block = self._tool_call_blocks.get(tc_id)
        if block is not None:
            block["tool_call"]["sources"] = sources

    def on_generation_done(self, duration: int):
        self.generation_duration += duration

    def on_error(self, error: str):
        """Append an error block."""
        self._close_text()
        self._close_thinking()
        self.blocks.append({"type": "error", "content": error})

    def get_text_content(self) -> str:
        """Concatenation of all text block contents (for the `content`
        column + tsvector search)."""
        return "".join(
            b.get("content", "") for b in self.blocks if b.get("type") == "text"
        )

    def to_db_blocks(self) -> list[dict]:
        """Return the blocks list for DB persistence (strips any internal
        state). Called after the stream completes."""
        # Close any dangling open blocks
        return self.blocks
