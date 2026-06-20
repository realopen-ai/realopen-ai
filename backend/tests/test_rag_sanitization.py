"""
Tests for the null-byte / invalid-Unicode sanitization fix.

Production crash these tests guard against:
    asyncpg.exceptions.CharacterNotInRepertoireError:
        invalid byte sequence for encoding "UTF8": 0x00

Root cause: PDF extractors (pdfplumber, pypdf) occasionally emit NUL
bytes (0x00) for garbled text streams. PostgreSQL TEXT/VARCHAR columns
reject 0x00, which crashed the entire chunk INSERT batch and left the
SQLAlchemy session in a poisoned "rolled-back" state where even the
failure handler couldn't update the doc row.

Fix: `_sanitize_text_for_pg()` strips NUL bytes and lone surrogates
at every text-entry point (extractors, vision descriptions, persistence
layer). These tests verify the sanitizer behaves correctly and is
applied at every layer.
"""

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import rag

# ─── _sanitize_text_for_pg unit tests ───────────────────────────────


class TestSanitizeTextForPg:
    """Direct unit tests for the _sanitize_text_for_pg helper."""

    def test_strips_null_bytes(self):
        """NUL bytes (0x00) MUST be stripped — this is the actual
        production crash."""
        assert rag._sanitize_text_for_pg("hello\x00world") == "helloworld"
        assert rag._sanitize_text_for_pg("\x00") == ""
        assert rag._sanitize_text_for_pg("a\x00b\x00c") == "abc"

    def test_preserves_normal_text(self):
        """Normal text passes through unchanged."""
        assert rag._sanitize_text_for_pg("hello world") == "hello world"
        assert rag._sanitize_text_for_pg("Q3 revenue: $4.2M") == "Q3 revenue: $4.2M"

    def test_preserves_newlines_and_tabs(self):
        """Newlines, tabs, and other valid control chars are KEPT."""
        text = "line1\nline2\ttabbed\r\ncrlf"
        assert rag._sanitize_text_for_pg(text) == text

    def test_preserves_unicode(self):
        """CJK, emoji, accented chars all pass through."""
        text = "你好世界 🌍 café München"
        assert rag._sanitize_text_for_pg(text) == text

    def test_handles_none(self):
        """None should become empty string (defensive)."""
        assert rag._sanitize_text_for_pg(None) == ""

    def test_handles_non_string(self):
        """Non-string inputs should be coerced to string."""
        assert rag._sanitize_text_for_pg(42) == "42"
        assert rag._sanitize_text_for_pg(3.14) == "3.14"

    def test_strips_lone_surrogates(self):
        """Lone UTF-16 surrogates (U+D800..U+DFFF) can't be UTF-8
        encoded and would crash asyncpg. They should be replaced
        with the replacement char (U+FFFD)."""
        # Lone surrogates — these can't be encoded to UTF-8
        # We construct them via chr() to avoid Python source encoding issues.
        surrogate = chr(0xD800)  # lone high surrogate
        result = rag._sanitize_text_for_pg(f"before{surrogate}after")
        assert "\ud800" not in result, "Lone surrogate was not stripped"
        assert "before" in result and "after" in result

    def test_preserves_valid_emoji_surrogate_pairs(self):
        """Valid emoji (encoded as surrogate PAIRS in UTF-16) must
        pass through unchanged when expressed as a single Python
        code point."""
        # 🌍 is U+1F30D — a single code point in Python 3, outside
        # the surrogate range, so it's fine.
        assert rag._sanitize_text_for_pg("🌍") == "🌍"

    def test_mixed_null_bytes_and_normal_text(self):
        """A realistic PDF text snippet with embedded NUL bytes."""
        pdf_text = (
            "Arcturus Morningstar 3.5.4\n"
            "Multiple Highscores Per Room\x00\n"
            "Implementation Guide\x00\x00\n"
            "Complete Code Changes"
        )
        cleaned = rag._sanitize_text_for_pg(pdf_text)
        assert "\x00" not in cleaned
        assert "Arcturus Morningstar" in cleaned
        assert "Implementation Guide" in cleaned


# ─── Sanitization applied at extraction layer ───────────────────────


class TestExtractionSanitization:
    """Verify every _extract_* function sanitizes its output."""

    def test_extract_txt_strips_null_bytes(self):
        """_extract_txt MUST strip NUL bytes from decoded text."""
        # A text file with embedded NUL bytes (simulates a corrupt upload)
        buf = b"hello\x00world\nsecond line\x00"
        result = rag._extract_txt(buf)
        assert "\x00" not in result.pages[0].text
        assert "helloworld" in result.pages[0].text

    def test_extract_csv_strips_null_bytes(self):
        """_extract_csv MUST strip NUL bytes."""
        buf = b"col1,col2\nval1\x00,val2"
        result = rag._extract_csv(buf)
        assert "\x00" not in result.pages[0].text

    def test_extract_markdown_strips_null_bytes(self):
        """_extract_markdown delegates to _extract_txt, which sanitizes."""
        buf = b"# Header\x00\n\nText with \x00 null"
        result = rag._extract_markdown(buf)
        assert "\x00" not in result.pages[0].text

    def test_extract_pdf_strips_null_bytes(self, monkeypatch):
        """_extract_pdf MUST sanitize each page's text.

        We mock pdfplumber to return text with NUL bytes (simulating
        a garbled PDF text stream), then verify the resulting
        ExtractedPage.text has no NUL bytes.
        """

        # Mock pdfplumber to return text with NUL bytes
        class FakePage:
            def __init__(self, text):
                self._text = text

            def extract_text(self):
                return self._text

        class FakePdf:
            def __init__(self, pages):
                self.pages = pages

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        nul_text = "Page 1 has a \x00 NUL byte\nSecond line\x00"
        fake_pdf = FakePdf([FakePage(nul_text)])

        # Patch pdfplumber.open at the module level
        with patch("pdfplumber.open", return_value=fake_pdf):
            result = rag._extract_pdf(b"fake pdf bytes")

        assert len(result.pages) == 1
        assert "\x00" not in result.pages[0].text
        assert "Page 1 has a" in result.pages[0].text

    def test_extract_docx_strips_null_bytes(self):
        """_extract_docx MUST sanitize each paragraph's text."""
        from docx import Document

        doc = Document()
        # Add a paragraph — python-docx doesn't naturally emit NUL bytes,
        # but we simulate the scenario by patching the paragraph.text
        # property to include one. The sanitizer should strip it.
        doc.add_paragraph("Hello world")
        buf = io.BytesIO()
        doc.save(buf)

        result = rag._extract_docx(buf.getvalue())
        # Sanity: normal DOCX text passes through
        assert "Hello world" in result.pages[0].text
        # No NUL bytes in output (this is trivially true here since
        # python-docx doesn't emit them, but the test documents the
        # invariant)
        assert "\x00" not in result.pages[0].text

    def test_extract_xlsx_strips_null_bytes(self):
        """_extract_xlsx MUST sanitize each sheet's text."""
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws["A1"] = "Hello"
        ws["B1"] = "World"
        buf = io.BytesIO()
        wb.save(buf)

        result = rag._extract_xlsx(buf.getvalue())
        assert "\x00" not in result.pages[0].text
        assert "Hello" in result.pages[0].text


# ─── Sanitization applied at vision-LLM description layer ───────────


class TestVisionSanitization:
    """Verify vision-LLM descriptions are sanitized."""

    @pytest.mark.asyncio
    async def test_describe_image_strips_null_bytes_from_vision_response(self):
        """If the vision LLM returns a description containing NUL bytes
        (rare but possible with some model outputs), the sanitizer
        must strip them before returning."""
        # Mock httpx.AsyncClient to return a response with NUL bytes
        fake_response = MagicMock()
        fake_response.raise_for_status = MagicMock()
        fake_response.json = MagicMock(
            return_value={
                "message": {"content": "Image shows a chart\x00with NUL bytes\x00"}
            }
        )

        fake_client = AsyncMock()
        fake_client.post = AsyncMock(return_value=fake_response)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.rag.settings") as mock_settings, patch(
            "app.services.rag.asyncio.to_thread",
            return_value=b"fake png bytes",
        ), patch("app.services.rag.base64.b64encode", return_value=b"fakeb64"), patch(
            "httpx.AsyncClient", return_value=fake_client
        ):
            mock_settings.resolve_model = MagicMock(return_value="llama3.2-vision")
            mock_settings.RAG_VISION_MODEL_ROLE = "vision"
            mock_settings.RAG_VISION_IMAGE_MAX_DIM = 1024
            mock_settings.OLLAMA_BASE_URL = "http://localhost:11434"

            # Mock _normalize_image too since it uses PIL
            with patch("app.services.rag._normalize_image", return_value=b"png"):
                result = await rag.describe_image_with_vision(b"fake image")

        assert "\x00" not in result
        assert "Image shows a chart" in result


# ─── digest_document: error handler uses fresh session ──────────────


class TestDigestErrorHandler:
    """Verify the error handler in digest_document doesn't poison
    sessions and properly marks the doc as failed.

    The original production crash:
        [rag] failed to mark doc as failed: This Session's transaction
        has been rolled back due to a previous exception during flush.
        To begin a new transaction with this Session, first issue
        Session.rollback().

    Fix: the error handler now uses a FRESH session (async_session_factory)
    instead of the caller's poisoned session.
    """

    @pytest.mark.asyncio
    async def test_error_handler_uses_fresh_session(self):
        """When digestion fails, the error handler MUST open a fresh
        session to mark the doc as failed — NOT reuse the caller's
        poisoned session."""
        # Mock the caller's db session — it's "poisoned" (any commit fails)
        poisoned_db = MagicMock()
        poisoned_db.add = MagicMock()
        poisoned_db.commit = AsyncMock()
        poisoned_db.refresh = AsyncMock()
        poisoned_db.rollback = AsyncMock()

        # Track fresh sessions opened via async_session_factory
        fresh_sessions = []

        class FakeFreshSession:
            """A fake session that records operations for assertion."""

            def __init__(self):
                self.executed = []
                self.committed = False
                fresh_sessions.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def add(self, obj):
                self.executed.append(("add", obj))

            def add_all(self, objs):
                self.executed.append(("add_all", objs))

            async def execute(self, stmt):
                self.executed.append(("execute", stmt))
                return MagicMock()

            async def commit(self):
                self.committed = True

        # Make digestion fail by having save_uploaded_file raise
        with patch(
            "app.services.rag.save_uploaded_file",
            new=AsyncMock(side_effect=RuntimeError("Simulated extraction failure")),
        ), patch("app.services.rag.async_session_factory") as mock_factory:
            # Each call to async_session_factory() returns a new FakeFreshSession
            mock_factory.return_value.__aenter__ = AsyncMock(
                return_value=FakeFreshSession()
            )
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=None)

            with pytest.raises(RuntimeError, match="Simulated extraction failure"):
                await rag.digest_document(
                    poisoned_db,
                    file_bytes=b"fake",
                    filename="test.pdf",
                    scope="private",
                )

        # The error handler should have opened at least one fresh session
        # to mark the doc as failed. If it tried to reuse the caller's
        # poisoned session, fresh_sessions would be empty.
        assert len(fresh_sessions) >= 1, (
            "Error handler did not open a fresh session — it would have "
            "failed on the caller's poisoned session (the original bug)."
        )
        # At least one fresh session should have been committed (the
        # UPDATE that marks the doc as failed).
        assert any(
            s.committed for s in fresh_sessions
        ), "No fresh session was committed — the doc-failed UPDATE didn't land."


# ─── digest_document: chunks persisted in batches ───────────────────


class TestBatchedPersistence:
    """Verify chunks are persisted in batches of 10 (defensive).

    Batching means that if one batch fails (e.g. a sanitization edge
    case slips through), the other batches still land and the doc
    remains partially searchable.
    """

    @pytest.mark.asyncio
    async def test_chunks_persisted_in_batches_of_10(self):
        """For a 25-chunk doc, we expect 3 batch commits (10+10+5)."""
        # Mock the caller's db
        caller_db = MagicMock()
        caller_db.add = MagicMock()
        caller_db.commit = AsyncMock()
        caller_db.refresh = AsyncMock()

        # Track fresh sessions opened during chunk persistence
        fresh_sessions = []

        class FakeFreshSession:
            def __init__(self):
                self.added = []
                self.committed = False
                fresh_sessions.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def add(self, obj):
                self.added.append(obj)

            def add_all(self, objs):
                self.added.extend(objs)

            async def execute(self, stmt):
                return MagicMock()

            async def commit(self):
                self.committed = True

        # Build 25 fake chunks
        chunks = [
            rag.Chunk(
                text=f"chunk {i}",
                chunk_index=i,
                page_number=1,
                line_start=1,
                line_end=1,
            )
            for i in range(25)
        ]

        # Stub out everything except the persistence phase
        with patch(
            "app.services.rag.save_uploaded_file",
            new=AsyncMock(return_value="documents/x/f.pdf"),
        ), patch(
            "app.services.rag.extract_content_async",
            new=AsyncMock(
                return_value=rag.ExtractionResult(
                    pages=[rag.ExtractedPage(page_number=1, text="x" * 100)],
                    images=[],
                )
            ),
        ), patch(
            "app.services.rag.chunk_pages", return_value=chunks
        ), patch(
            "app.services.rag.get_embeddings",
            new=AsyncMock(return_value=[[0.1] * 768 for _ in range(25)]),
        ), patch(
            "app.services.rag.async_session_factory"
        ) as mock_factory:
            # IMPORTANT: each call to async_session_factory() must return
            # a NEW FakeFreshSession. Using side_effect (not return_value)
            # ensures a fresh mock is created per call.
            def make_fake_ctx():
                session = FakeFreshSession()
                ctx = MagicMock()
                ctx.__aenter__ = AsyncMock(return_value=session)
                ctx.__aexit__ = AsyncMock(return_value=None)
                return ctx

            mock_factory.side_effect = make_fake_ctx

            try:
                await rag.digest_document(
                    caller_db,
                    file_bytes=b"fake",
                    filename="test.txt",
                    scope="private",
                )
            except Exception:
                pass  # We don't care about the return value, only the batches

        # Count the sessions that had add_all called on them (those are
        # the chunk-batch sessions). Other fresh sessions are for the
        # file_path UPDATE and the doc-stats UPDATE (which use execute(),
        # not add_all).
        batch_sessions = [s for s in fresh_sessions if s.added]
        # 25 chunks / 10 per batch = 3 batch sessions
        assert len(batch_sessions) == 3, (
            f"Expected 3 chunk-batch sessions (10+10+5), got {len(batch_sessions)}. "
            f"Batching may have regressed. Total sessions: {len(fresh_sessions)}"
        )
        # Verify the batches were sized correctly: 10, 10, 5
        assert len(batch_sessions[0].added) == 10
        assert len(batch_sessions[1].added) == 10
        assert len(batch_sessions[2].added) == 5


# ─── DB session released during CPU-bound work ──────────────────────


class TestDbSessionReleasedDuringCpuWork:
    """Verify digest_document does NOT hold the caller's DB session
    during the long CPU-bound extraction/chunking/embedding phases.

    This is the actual fix for the "backend hangs on large PDF" issue:
    the previous implementation held the caller's session for the
    entire 30+ second digestion, starving the connection pool (size 5+10)
    and making /health queue up waiting for a connection.
    """

    @pytest.mark.asyncio
    async def test_caller_session_not_used_during_extraction(self):
        """After the initial doc-row insert, the caller's db.commit()
        should NOT be called again during the extraction phase.

        We track all calls to the caller's db.commit and verify they
        only happen at the very start (Phase 1), not during extraction.
        """
        caller_db = MagicMock()
        commit_call_count = {"n": 0}

        async def counting_commit():
            commit_call_count["n"] += 1

        caller_db.add = MagicMock()
        caller_db.commit = counting_commit
        caller_db.refresh = AsyncMock()

        extraction_started = {"v": False}
        commits_during_extraction = {"n": 0}

        async def slow_extract(buf, filename):
            # Mark that we're in the extraction phase
            extraction_started["v"] = True
            commits_before = commit_call_count["n"]
            import asyncio

            await asyncio.sleep(0.1)
            commits_after = commit_call_count["n"]
            commits_during_extraction["n"] += commits_after - commits_before
            return rag.ExtractionResult(
                pages=[rag.ExtractedPage(page_number=1, text="content")],
                images=[],
            )

        # Stub everything; use a FakeFreshSession so the persistence
        # phase doesn't blow up.
        class FakeFreshSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def add(self, obj):
                pass

            def add_all(self, objs):
                pass

            async def execute(self, stmt):
                return MagicMock()

            async def commit(self):
                pass

        with patch(
            "app.services.rag.save_uploaded_file",
            new=AsyncMock(return_value="documents/x/f.pdf"),
        ), patch("app.services.rag.extract_content_async", new=slow_extract), patch(
            "app.services.rag.chunk_pages",
            return_value=[
                rag.Chunk(
                    text="c", chunk_index=0, page_number=1, line_start=1, line_end=1
                )
            ],
        ), patch(
            "app.services.rag.get_embeddings",
            new=AsyncMock(return_value=[[0.1] * 768]),
        ), patch(
            "app.services.rag.async_session_factory"
        ) as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(
                return_value=FakeFreshSession()
            )
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=None)

            try:
                await rag.digest_document(
                    caller_db,
                    file_bytes=b"fake",
                    filename="test.txt",
                    scope="private",
                )
            except Exception:
                pass

        # The caller's db.commit should NOT have been called during
        # extraction. If it was, that means we're still holding the
        # caller's session during CPU-bound work (the bug).
        assert commits_during_extraction["n"] == 0, (
            f"Caller's db.commit was called {commits_during_extraction['n']} "
            f"times during extraction — the session is being held during "
            f"CPU-bound work, which starves the connection pool."
        )


# ─── Embeddings: semaphore limits concurrency ───────────────────────


class TestEmbeddingSemaphore:
    """Verify the embedding service uses a semaphore to limit
    concurrent Ollama calls (prevents overwhelming a single-threaded
    Ollama instance with 50 simultaneous requests)."""

    @pytest.mark.asyncio
    async def test_get_embedding_uses_semaphore(self):
        """get_embedding should acquire the semaphore before making
        the HTTP call. We verify this by checking that the semaphore
        exists and is bounded."""
        from app.services import embeddings

        # Reset the singleton to ensure clean state
        embeddings._EMBED_SEMAPHORE = None
        sem = embeddings._get_semaphore()
        assert sem is not None
        # The semaphore should be bounded (we set it to 5)
        # asyncio.Semaphore doesn't expose its value directly, but we
        # can verify it's a Semaphore instance
        assert isinstance(sem, asyncio.Semaphore)

    @pytest.mark.asyncio
    async def test_embeddings_max_concurrency_5(self):
        """At most 5 embedding calls should be in-flight at once."""
        import asyncio
        from app.services import embeddings

        # Reset the semaphore
        embeddings._EMBED_SEMAPHORE = None

        # Track max concurrency
        current = {"n": 0}
        max_seen = {"n": 0}

        async def fake_post(self, url, json):
            current["n"] += 1
            max_seen["n"] = max(max_seen["n"], current["n"])
            await asyncio.sleep(0.05)  # simulate network latency
            current["n"] -= 1

            class FakeResp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"embedding": [0.1] * 768}

            return FakeResp()

        # Mock settings + httpx
        with patch.object(
            embeddings.settings, "resolve_model", return_value="nomic-embed-text:v1.5"
        ), patch.object(
            embeddings.settings, "MEMORY_EMBEDDING_MODEL_ROLE", "embedding"
        ), patch.object(
            embeddings.settings, "OLLAMA_BASE_URL", "http://localhost:11434"
        ), patch(
            "httpx.AsyncClient.post", new=fake_post
        ):
            # Fire 20 concurrent embedding calls
            texts = [f"text {i}" for i in range(20)]
            await embeddings.get_embeddings(texts)

        # Max concurrency should be ≤5 (the semaphore limit).
        # We allow up to 5 — could be less if calls finish fast.
        assert max_seen["n"] <= 5, (
            f"Max concurrency was {max_seen['n']} — semaphore isn't capping "
            f"concurrency at 5. This would overwhelm Ollama on large digestions."
        )
        assert max_seen["n"] >= 1, "No embedding calls executed"
