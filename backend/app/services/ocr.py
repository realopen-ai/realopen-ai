"""Local, engine-neutral OCR. No network/model calls; bounded subprocesses."""

import asyncio
import csv
import io
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from PIL import Image


@dataclass
class OCRResult:
    text: str
    engine: str
    language: str
    words: list[dict] = field(default_factory=list)


class OCREngine(Protocol):
    def recognize(self, image: bytes, language: str) -> OCRResult: ...


def installed_languages() -> set[str]:
    """Query actual trained-data availability, not just engine installation."""
    if not shutil.which("tesseract"):
        return set()
    try:
        result = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() in {"eng", "fra", "ara"}
        }
    except (OSError, subprocess.SubprocessError):
        return set()


def default_language() -> str:
    installed = installed_languages()
    return (
        "+".join(language for language in ("eng", "fra", "ara") if language in installed) or "eng"
    )


class TesseractEngine:
    def recognize(self, image: bytes, language: str = "eng") -> OCRResult:
        if not shutil.which("tesseract"):
            raise RuntimeError("Tesseract is not installed")
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
        with Image.open(io.BytesIO(image)) as picture:
            if picture.width * picture.height > 20_000_000:
                raise ValueError("OCR image exceeds pixel limit")
            with tempfile.TemporaryDirectory(prefix="realopen-ocr-") as tmp:
                path = Path(tmp) / "page.png"
                picture.convert("RGB").save(path)
                result = subprocess.run(
                    ["tesseract", str(path), "stdout", "-l", language, "tsv"],
                    capture_output=True,
                    timeout=60,
                    check=True,
                )
        words, lines = [], {}
        for row in csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t"):
            if not row.get("text", "").strip():
                continue
            word = {
                "text": row["text"],
                "confidence": float(row["conf"]),
                "box": [int(row[k]) for k in ("left", "top", "width", "height")],
            }
            words.append(word)
            key = tuple(row[k] for k in ("page_num", "block_num", "par_num", "line_num"))
            lines.setdefault(key, []).append(row["text"])
        return OCRResult(
            "\n".join(" ".join(line) for line in lines.values()),
            "tesseract",
            language,
            words,
        )


_slots = asyncio.Semaphore(2)
default_engine: OCREngine = TesseractEngine()


async def recognize(image: bytes, language="eng", *, engine: OCREngine | None = None):
    if len(image) > 20 * 1024 * 1024:
        raise ValueError("OCR image exceeds byte limit")
    async with _slots:
        return await asyncio.to_thread((engine or default_engine).recognize, image, language)
