"""
Pytest fixtures + environment setup for the RAG test suite.

These tests are designed to run without a live database or Ollama — they
exercise the pure-function pieces of the RAG pipeline (extraction,
chunking, source formatting) and use unittest.mock to stub out the
embedding + DB layers for the search/ingestion flow tests.

The DATABASE_URL env var must be set BEFORE app.db.session is imported,
otherwise create_async_engine() raises at import time. We FORCE a
placeholder postgres URL — it's never actually used because each test
that would touch the DB mocks the session factory. We use os.environ[]
(not setdefault) because the shell environment may already have a
DATABASE_URL pointing at a SQLite file (from a sibling project) that
asyncpg can't parse.
"""

import os
import sys
from pathlib import Path

# Force a placeholder DATABASE_URL so create_async_engine doesn't blow up
# at import time. The tests that need a real DB session mock
# async_session_factory, so this URL is never actually connected to.
# We use postgresql+asyncpg because asyncpg is installed (the real
# production driver) — create_async_engine is lazy, so it won't actually
# try to connect until a session is opened (which our tests never do).
os.environ["DATABASE_URL"] = "postgresql+asyncpg://test:test@localhost:5432/test"
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("HARDWARE_PROFILE", "cpu_small")

# Make sure the backend app package is importable when running tests
# from the backend/ directory (or from the project root).
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
