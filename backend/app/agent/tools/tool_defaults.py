"""Tool loading policy defaults (Brain ▸ Tools).

These two structures were previously hardcoded inside
app/agent/service.py. They moved here because they are now the DEFAULT
tool-availability policy used to SEED each tool's configuration on
startup — the runtime reads the persisted per-tool configuration
(see config_store), whose defaults derive from this module.

Semantics (unchanged from the original mechanism):

- ``_ALWAYS_TOOLS``: tools included in the LLM tool list on every turn
  regardless of the message content. These become ``always_load=True``
  defaults.

- ``_KEYWORD_TOOLS``: keyword → tools mapping. A tag-gated tool
  (``always_load=False``) is only offered to the LLM when one of its
  TAGS appears in the user's message. The per-tool default tag list
  is derived from this mapping.

Adding a new tool: give it a ToolConfigDefinition (config_base) — or
rely on the automatic basic config — and, if it should be tag-gated
by default, add its keywords to _KEYWORD_TOOLS.
"""

from typing import Dict, List, Set

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
# The keyword vocabulary doubles as the per-tool default TAGS (the
# tag-gating replaces the old comma-separated keyword_gate string).
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
    "image": {"use_vision", "use_image_gen"},
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
    "imagine": {"use_image_gen"},
    # Report generation triggers
    "report": {"use_report_gen"},
    "deliverable": {"use_report_gen", "use_pptx_gen", "use_excel_gen"},
    "generate": {"use_report_gen", "use_image_gen", "use_pptx_gen", "use_excel_gen"},
    "create": {"use_report_gen", "use_image_gen", "use_pptx_gen", "use_excel_gen"},
    # Presentation generation triggers
    "presentation": {"use_pptx_gen"},
    "slides": {"use_pptx_gen"},
    "slideshow": {"use_pptx_gen"},
    "pptx": {"use_pptx_gen"},
    "deck": {"use_pptx_gen"},
    "powerpoint": {"use_pptx_gen"},
    # Excel generation triggers
    "excel": {"use_excel_gen"},
    "spreadsheet": {"use_excel_gen"},
    "xlsx": {"use_excel_gen"},
    "xls": {"use_excel_gen"},
    "workbook": {"use_excel_gen"},
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


def default_always_load(tool_name: str) -> bool:
    """Default always_load for a tool (current-behavior preserving)."""
    return tool_name in _ALWAYS_TOOLS


def default_tags(tool_name: str) -> List[str]:
    """Default activation tags for a tool, derived from the keyword map.

    A tag-gated tool (always_load=False) is offered to the LLM only
    when the user's message contains one of these tags.
    """
    return sorted(kw for kw, tools in _KEYWORD_TOOLS.items() if tool_name in tools)
