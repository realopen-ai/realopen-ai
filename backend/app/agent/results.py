"""Loss-aware model views; raw results remain in UI, persistence and traces."""

import json
from dataclasses import dataclass


def reference_metadata(data):
    """Keep identifiers/locations and action metadata even between large bodies."""
    if not isinstance(data, dict):
        return {}
    return {
        key: value
        for key, value in data.items()
        if (
            key.endswith("_id")
            or key.endswith("_url")
            or key.endswith("_count")
            or key
            in {
                "id",
                "name",
                "title",
                "type",
                "kind",
                "artifact_kind",
                "status",
                "version",
                "offset",
                "next_offset",
                "exit_code",
                "page",
                "page_number",
                "line_start",
                "line_end",
                "filename",
                "file_path",
                "files_changed",
                "sections",
            }
        )
        and isinstance(value, (str, int, float, bool, list, type(None)))
    }


@dataclass(frozen=True)
class ResultView:
    content: str
    shortened: bool = False


def compact_result(
    name, output, call=None, *, success=True, result_ref=None, limit=6000
):
    """Never shorten error diagnostics, loaded instructions or a requested page.

    Shortened evidence is explicit and recoverable via request-local paging.
    No summarizer, semantic rewriting, or lost IDs/citation headers.
    """
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    if (
        not success
        or name in {"load_skill", "read_tool_result"}
        or len(text) <= limit
        or not result_ref
    ):
        return ResultView(text)
    limit = max(1000, limit)
    sources = getattr(call, "rag_sources", None) if call else None
    web = getattr(call, "web_results", None) if call else None
    if sources or web:
        records = []
        items = sources or web
        excerpt_size = max(80, (limit - len(items) * 320) // max(1, len(items)))
        for index, item in enumerate(items):
            keys = (
                (
                    "document_id",
                    "document_filename",
                    "chunk_id",
                    "page_number",
                    "line_start",
                    "line_end",
                    "chunk_type",
                )
                if sources
                else ("title", "url", "provider")
            )
            record = {key: item[key] for key in keys if key in item}
            body = str(item.get("text", item.get("snippet", "")))
            record.update(
                source=index + 1,
                excerpt=body[:excerpt_size],
                excerpt_shortened=len(body) > excerpt_size,
            )
            records.append(record)
        data = {"sources" if sources else "results": records}
    else:
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
            # Exact artifact page: preserve structural references and advance
            # the real character offset, not a guessed section/page number.
            data = {key: value for key, value in parsed.items() if key != "content"}
            allowance = max(
                200, limit - len(json.dumps(data, ensure_ascii=False)) - 400
            )
            body = parsed["content"]
            data["content"] = body[:allowance]
            if len(body) > allowance and isinstance(parsed.get("offset"), int):
                data["next_offset"] = parsed["offset"] + allowance
        else:
            # Head + tail retain command summaries and terminal diagnostics;
            # the middle is never represented as a complete result.
            allowance = max(200, limit - 600)
            data = {
                "excerpt_start": text[: allowance // 2],
                "excerpt_end": text[-allowance // 2 :],
                "metadata": reference_metadata(parsed),
            }
    data.update(
        shortened=True,
        result_ref=result_ref,
        original_chars=len(text),
        message="This is a shortened model view, not the complete output. Read omitted evidence with read_tool_result(result_ref); it defaults to offset 0 and 3000 characters. Continue with next_offset; offsets/limits are characters, not pages or lines.",
    )
    return ResultView(json.dumps(data, ensure_ascii=False, separators=(",", ":")), True)
