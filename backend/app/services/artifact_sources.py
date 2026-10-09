"""Lossless generator source capture and deterministic section boundaries."""

import json
import re
from datetime import date, datetime
from pathlib import Path


def sections_for(source: str, kind: str) -> list[dict]:
    if kind == "excel":
        spec = json.loads(source)
        return [
            {
                "id": f"sheet-{i + 1}",
                "title": sheet["name"],
                "sheet": sheet["name"],
                "content": json.dumps(sheet, ensure_ascii=False, indent=2),
            }
            for i, sheet in enumerate(spec["sheets"])
        ]
    if kind == "presentation":
        blocks = re.split(r"(?m)^\s*---\s*$", source)
        return [
            {
                "id": f"slide-{i + 1}",
                "title": next(
                    (line.lstrip("# ") for line in block.splitlines() if line.strip()),
                    f"Slide {i + 1}",
                ),
                "slide": i + 1,
                "content": block,
            }
            for i, block in enumerate(blocks)
            if block.strip()
        ]
    starts = [match.start() for match in re.finditer(r"(?m)^#{1,2} .+$", source)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    starts.append(len(source))
    return [
        {
            "id": f"section-{i + 1}",
            "title": source[start:end].splitlines()[0].lstrip("# ")
            if source[start:end].strip()
            else "Introduction",
            "content": source[start:end],
        }
        for i, (start, end) in enumerate(zip(starts, starts[1:]))
        if start < end
    ]


def capture_source(directory: Path, report_id: str, source: str, kind: str, settings: dict):
    # Sidecar remains available if a generation completes before metadata is
    # committed. Never expose source in chat/SSE or regenerate it using an LLM.
    payload = {"source": source, "kind": kind, "settings": settings}
    (directory / f"{report_id}.source.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def workbook_json(spec):
    def encode(value):
        if isinstance(value, datetime):
            return {"$datetime": value.isoformat()}
        if isinstance(value, date):
            return {"$date": value.isoformat()}
        raise TypeError(f"Unsupported workbook value: {type(value).__name__}")

    return json.dumps(spec, ensure_ascii=False, default=encode)


def workbook_spec(source):
    def decode(value):
        if set(value) == {"$date"}:
            return date.fromisoformat(value["$date"])
        if set(value) == {"$datetime"}:
            return datetime.fromisoformat(value["$datetime"])
        return value

    return json.loads(source, object_hook=decode)
