"""Bounded, deterministic evidence handoff; no summarizer or model evaluation."""

import json

from app.agent.results import reference_metadata


def evidence_records(result_ref, tool, arguments, result):
    """Keep successful source-tool outputs, never memories or action receipts."""
    if not result.success or not tool.provides_evidence:
        return []
    provenance = {
        "result_ref": result_ref,
        "tool": tool.name,
        "request": {
            key: value
            for key, value in arguments.items()
            if key
            in {
                "query",
                "url",
                "artifact_id",
                "section_id",
                "version",
                "offset",
                "question",
            }
        },
    }
    call = result.tool_call
    sources = (
        (getattr(call, "rag_sources", None) or getattr(call, "web_results", None))
        if call
        else None
    )
    if sources:
        return [
            {
                **provenance,
                "source": index + 1,
                "metadata": {
                    key: value
                    for key, value in source.items()
                    if key not in {"text", "snippet"}
                },
                "text": str(source.get("text", source.get("snippet", ""))),
            }
            for index, source in enumerate(sources)
        ]
    text = (
        result.output
        if isinstance(result.output, str)
        else json.dumps(result.output, ensure_ascii=False, default=str)
    )
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None
    kind = "retrieved_source"
    if isinstance(value, dict):
        if "sections" in value and "content" not in value:
            kind = "outline_only"
        elif value.get("artifact_kind") in {"report", "presentation", "excel"}:
            kind = "generated_draft"
    return [
        {
            **provenance,
            "metadata": reference_metadata(value),
            "evidence_kind": kind,
            "text": text,
        }
    ]


def bounded_evidence(records, limit=12000):
    """Balance exact excerpts across sources, retaining provenance and omissions.

    Full outputs remain in the normal tool result store/UI/traces. This is only
    the report writer's bounded view, not a claim that sources were verified.
    """
    selected = records[-24:]

    def render(size):
        return json.dumps(
            {
                "omitted_sources": len(records) - len(selected),
                "sources": [
                    {
                        **{key: value for key, value in item.items() if key != "text"},
                        "excerpt": item["text"][:size],
                        "original_chars": len(item["text"]),
                        "excerpt_shortened": len(item["text"]) > size,
                    }
                    for item in selected
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    # Metadata itself can be large. Drop oldest records explicitly rather than
    # cutting a URL/UUID/page reference or emitting malformed JSON.
    while selected and len(render(0)) > limit:
        selected = selected[1:]
    low, high = 0, min(limit, max((len(item["text"]) for item in selected), default=0))
    while low < high:
        middle = (low + high + 1) // 2
        if len(render(middle)) <= limit:
            low = middle
        else:
            high = middle - 1
    return render(low)
