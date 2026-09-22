"""Sentence chunker for streaming TTS + TTS text cleaning.

The agent streams text token-by-token; TTS needs sentence-sized units.
:class:`SentenceChunker` accumulates tokens and emits a chunk when one of
these fires:

1. A sentence boundary (. ! ? … ; or a blank line) is found AND the
   accumulated text is at least ``min_chars`` long;
2. The accumulated text reaches ``max_chars`` (emit what's there, breaking
   at the last space so words are never split);
3. The flush timeout — ``flush_ms`` since the first buffered character —
   fires (checked on feed/flush calls; the voice session also polls it
   while waiting for the next token). The timeout flush only fires once
   at least ``min_chars`` are buffered (config comment for
   ``VOICE_TTS_MIN_CHARS``: "minimum chars before a timeout flush may
   fire").

``flush()`` emits any remaining text regardless of length (end of turn).

:func:`clean_for_tts` strips markdown/code-block noise before synthesis —
TTS should speak prose, not syntax.
"""

from __future__ import annotations

import re
import time
from typing import List, Optional

from app.config import settings

# Sentence-boundary: a terminator followed by whitespace, or a blank line.
# The trailing whitespace is REQUIRED so decimals ("3.14") and ellipses
# mid-word never split a chunk; a boundary at the very end of the buffer
# without following whitespace is picked up by the next token or flush().
_SENTENCE_BOUNDARY = re.compile(r"[.!?…;][ \t\r\n]+|\n[ \t]*\n")


class SentenceChunker:
    """Accumulates streamed tokens, emits TTS-sized sentence chunks."""

    def __init__(
        self,
        min_chars: Optional[int] = None,
        max_chars: Optional[int] = None,
        flush_ms: Optional[int] = None,
    ):
        if min_chars is None:
            min_chars = settings.VOICE_TTS_MIN_CHARS
        if max_chars is None:
            max_chars = settings.VOICE_TTS_MAX_CHARS
        if flush_ms is None:
            flush_ms = settings.VOICE_TTS_FLUSH_MS
        if min_chars < 1:
            raise ValueError("min_chars must be >= 1")
        if max_chars < min_chars:
            raise ValueError("max_chars must be >= min_chars")
        self.min_chars = int(min_chars)
        self.max_chars = int(max_chars)
        self.flush_ms = int(flush_ms)

        self._buffer: str = ""
        self._first_char_at: Optional[float] = None  # monotonic

    # ── public API ───────────────────────────────────────────────────

    def feed(self, token: str) -> List[str]:
        """Accumulate one agent token; return any chunks it completed.

        Feeding "" is allowed — it only re-checks the flush timeout (used
        by the TTS worker while waiting for the next token).
        """
        if token:
            self._buffer += token
            if self._first_char_at is None and self._buffer.strip():
                self._first_char_at = time.monotonic()
        return self._drain()

    def flush(self) -> List[str]:
        """Emit the remaining buffered text (end of generation)."""
        out: List[str] = []
        text = self._buffer.strip()
        if text:
            out.append(text)
        self._buffer = ""
        self._first_char_at = None
        return out

    def reset(self) -> None:
        """Drop everything (new generation / barge-in)."""
        self._buffer = ""
        self._first_char_at = None

    @property
    def buffered(self) -> str:
        """Currently buffered (unemitted) text — introspection/tests."""
        return self._buffer

    # ── internals ────────────────────────────────────────────────────

    def _drain(self) -> List[str]:
        out: List[str] = []
        out.extend(self._drain_sentence_boundaries())
        out.extend(self._drain_max_chars())
        out.extend(self._drain_timeout())
        # Empty buffer → reset the flush timer so the NEXT sentence's
        # timeout starts when its first char arrives.
        if not self._buffer.strip():
            self._buffer = ""
            self._first_char_at = None
        return [c for c in out if c.strip()]

    def _drain_sentence_boundaries(self) -> List[str]:
        """Rule 1: boundary + ≥min_chars. Skips short prefixes so a later
        boundary in the same buffer can still qualify."""
        out: List[str] = []
        while True:
            for match in _SENTENCE_BOUNDARY.finditer(self._buffer):
                pos = match.end()
                if len(self._buffer[:pos].strip()) >= self.min_chars:
                    chunk = self._buffer[:pos].strip()
                    self._buffer = self._buffer[pos:]
                    out.append(chunk)
                    break
            else:
                return out

    def _drain_max_chars(self) -> List[str]:
        """Rule 2: hard cap — break at the last space inside max_chars."""
        out: List[str] = []
        while len(self._buffer) >= self.max_chars:
            cut = self._buffer.rfind(" ", 0, self.max_chars)
            if cut <= 0:
                cut = self.max_chars
            chunk = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut:]
            if chunk:
                out.append(chunk)
        return out

    def _drain_timeout(self) -> List[str]:
        """Rule 3: flush_ms since the first buffered char (≥min_chars)."""
        if self._first_char_at is None:
            return []
        elapsed_ms = (time.monotonic() - self._first_char_at) * 1000.0
        if elapsed_ms < self.flush_ms:
            return []
        if len(self._buffer.strip()) < self.min_chars:
            return []
        chunk = self._buffer.strip()
        self._buffer = ""
        self._first_char_at = None
        return [chunk] if chunk else []


# ── Markdown / code noise stripping ──────────────────────────────────

# Fenced code blocks (``` or ~~~), possibly with a language tag.
_RE_FENCE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
# Images before links (so ![alt](url) isn't half-processed).
_RE_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
# Links: [text](url) → text
_RE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# Inline code: `code` (and double-backtick variant)
_RE_INLINE_CODE = re.compile(r"``([^`]+)``|`([^`]+)`")
# ATX headers: ## Title → Title
_RE_HEADER = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
# Emphasis / strong / strikethrough markers
_RE_STRONG = re.compile(r"(\*\*|__)(.*?)\1")
_RE_EMPH = re.compile(r"(\*|_)([^*_\n]+)\1")
_RE_STRIKE = re.compile(r"~~(.*?)~~")
# Bullet markers (- * + •) and ordered list markers (1. / 1))
_RE_BULLET = re.compile(r"^[ \t]*(?:[-*+•]|\d+[.)])[ \t]+", re.MULTILINE)
# Blockquotes
_RE_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
# Horizontal rules (--- *** ___)
_RE_HRULE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$", re.MULTILINE)
# Footnote-style markers [^1]
_RE_FOOTNOTE = re.compile(r"\[\^[^\]]*\]")


def clean_for_tts(text: str) -> str:
    """Strip markdown/code-block noise so TTS speaks prose, not syntax.

    Fenced code blocks are removed entirely (their prose description, if
    any, lives in the surrounding text); inline code, links, emphasis,
    headers and list markers are reduced to their spoken-word content.
    """
    if not text:
        return ""
    out = text
    out = _RE_FENCE.sub(" ", out)
    out = _RE_IMAGE.sub(" ", out)
    out = _RE_LINK.sub(r"\1", out)
    out = _RE_INLINE_CODE.sub(r"\1\2", out)
    out = _RE_FOOTNOTE.sub(" ", out)
    out = _RE_HEADER.sub("", out)
    out = _RE_STRONG.sub(r"\2", out)
    out = _RE_EMPH.sub(r"\2", out)
    out = _RE_STRIKE.sub(r"\1", out)
    out = _RE_BULLET.sub("", out)
    out = _RE_QUOTE.sub("", out)
    out = _RE_HRULE.sub(" ", out)
    # Collapse runs of whitespace (keep single newlines → single spaces).
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\s*\n\s*", " ", out)
    return out.strip()
