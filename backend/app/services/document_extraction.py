"""Reusable, ordered extraction snapshot, independent of embeddings."""

import asyncio
import hashlib
import json
import os
import uuid
import time
from pathlib import Path
from app.services import ocr
from app.services.rag import extract_content_async
from app.services.artifact_sources import sections_for
import fitz


async def extract(buf: bytes, filename: str, language=None):
    language = (
        language
        or os.environ.get("REALOPEN_OCR_LANGUAGE")
        or (
            await asyncio.to_thread(ocr.default_language)
            if filename.lower().endswith(".pdf")
            else "eng"
        )
    )
    if language not in {
        "eng",
        "fra",
        "ara",
        "eng+fra",
        "eng+ara",
        "fra+ara",
        "eng+fra+ara",
    }:
        raise ValueError("Unsupported OCR language")

    if len(buf) > 50 * 1024 * 1024:
        raise ValueError("Artifact extraction supports files up to 50 MB")
    if filename.lower().endswith(".pdf"):

        def check_pages():
            with fitz.open(stream=buf, filetype="pdf") as pdf:
                if pdf.page_count > 500:
                    raise ValueError("PDF exceeds 500-page extraction limit")

        await asyncio.to_thread(check_pages)
    result = await extract_content_async(buf, filename)
    methods, warnings, words = {}, [], {}
    if filename.lower().endswith(".pdf"):

        def render(page_number):
            with fitz.open(stream=buf, filetype="pdf") as pdf:
                if pdf.page_count > 500:
                    raise ValueError("PDF exceeds 500-page extraction limit")
                page = pdf[page_number - 1]
                scale = min(2, (20_000_000 / max(1, page.rect.width * page.rect.height)) ** 0.5)
                return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")

        deadline = time.monotonic() + 180
        for page in result.pages:
            methods[page.page_number] = "native"
            if len(page.text.strip()) < 30:
                if time.monotonic() >= deadline:
                    warnings.append(f"Page {page.page_number}: OCR time budget exceeded")
                    continue
                try:
                    recognized = await ocr.recognize(
                        await asyncio.to_thread(render, page.page_number), language
                    )
                    if recognized.text.strip():
                        page.text = recognized.text
                        methods[page.page_number] = "ocr"
                        words[page.page_number] = recognized.words
                except Exception as exc:
                    # An unavailable OCR engine must not destroy native extraction.
                    warnings.append(
                        f"Page {page.page_number}: OCR unavailable ({type(exc).__name__})"
                    )
    ext = Path(filename).suffix.lower()
    sections = []
    for page in result.pages:
        title = f"Page {page.page_number}"
        refs = {"page": page.page_number}
        if ext == ".pptx":
            refs = {"slide": page.page_number}
            title = f"Slide {page.page_number}"
        elif ext == ".xlsx":
            title = (
                page.text.splitlines()[0].removeprefix("# Sheet: ")
                if page.text
                else f"Sheet {page.page_number}"
            )
            refs = {"sheet": title}
        elif ext in {".txt", ".md", ".docx", ".csv"}:
            title = filename
            refs = {}
        if ext in {".md", ".docx"}:
            for part in sections_for(page.text, "report"):
                sections.append(
                    {
                        **part,
                        "id": f"part-{len(sections) + 1}",
                        "method": "native",
                        "ocr_words": [],
                    }
                )
            continue
        sections.append(
            {
                "id": f"part-{page.page_number}",
                "title": title,
                "content": page.text,
                **refs,
                "method": methods.get(page.page_number, "native"),
                "ocr_words": words.get(page.page_number, []),
            }
        )
    return result, {
        "hash": hashlib.sha256(buf).hexdigest(),
        "language": language,
        "sections": sections,
        "warnings": warnings,
        "schema_version": 1,
        "images": [
            {"index": index + 1, "page": image.page_number, "format": image.format_hint}
            for index, image in enumerate(result.images)
        ],
    }


def save_snapshot(path: Path, snapshot: dict):
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
