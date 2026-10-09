"""Bounded hierarchical summarization. Exact reads never call a model."""

import json


async def summarize_sections(sections, question, ask, progress=None):
    parts, missing, batches, current, size = [], [], [], [], 0
    for section in sections:
        text = section["content"]
        if not text.strip():
            missing.append(section["id"])
            continue
        marker = json.dumps(
            {
                key: section[key]
                for key in ("id", "title", "page", "slide", "sheet", "method")
                if key in section
            },
            ensure_ascii=False,
        )
        for start in range(0, len(text), 8000):
            part = f"Source: {marker}\n<source_data>\n{text[start : start + 8000]}\n</source_data>"
            if current and size + len(part) > 10000:
                batches.append("\n".join(current))
                current, size = [], 0
            current.append(part)
            size += len(part)
    if current:
        batches.append("\n".join(current))
    if not batches:
        raise ValueError("No readable content to summarize")
    for index, batch in enumerate(batches):
        if progress:
            await progress({"stage": "batches", "completed": index, "total": len(batches)})
        summary = await ask(
            f"Task: {question}\nSummarize concisely, retaining source markers.\n{batch}"
        )
        if not summary:
            raise RuntimeError("Model returned an empty summary")
        parts.append(summary)
        if progress:
            await progress({"stage": "batches", "completed": index + 1, "total": len(batches)})
    while len(parts) > 1:
        groups, current, size = [], [], 0
        for part in parts:
            if current and size + len(part) > 12000:
                groups.append(current)
                current, size = [], 0
            current.append(part)
            size += len(part)
        if current:
            groups.append(current)
        # Every reduction combines at least two bounded inputs.
        if len(groups) == len(parts):
            groups = [parts[i : i + 2] for i in range(0, len(parts), 2)]
        reduced = []
        for index, group in enumerate(groups):
            if progress:
                await progress({"stage": "synthesis", "completed": index, "total": len(groups)})
            summary = await ask(
                f"Task: {question}\nCombine these source summaries; retain source markers and qualifications.\n"
                + "\n".join(group)
            )
            if not summary:
                raise RuntimeError("Model returned an empty summary")
            reduced.append(summary)
        parts = reduced
    if progress:
        await progress({"stage": "complete", "completed": len(batches), "total": len(batches)})
    references = [
        {key: section[key] for key in ("id", "title", "page", "slide", "sheet") if key in section}
        for section in sections
        if section["content"].strip()
    ]
    # Deterministic provenance survives even when a small model drops citations.
    labels = [
        f"page {ref['page']}"
        if "page" in ref
        else f"slide {ref['slide']}"
        if "slide" in ref
        else f"sheet {ref['sheet']}"
        if "sheet" in ref
        else ref["id"]
        for ref in references
    ]
    return {
        "summary": parts[0] + "\n\nSource coverage (not per-claim citations): " + ", ".join(labels),
        "source_references": references,
        "source_coverage": ", ".join(labels),
        "missing_sections": missing,
        "sections_processed": len(sections) - len(missing),
    }
