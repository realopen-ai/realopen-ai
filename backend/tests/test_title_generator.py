"""
Tests for the LLM-powered conversation auto-titling service.

Layers:
1. _clean_title unit tests — the small-model output pathologies (think tags,
   fences, quotes, labels, trailing punctuation, word/char caps).
2. _fallback_title unit tests — truncation fallback when the LLM fails.
3. generate_title — httpx mocked; verifies the Ollama payload (model role
   resolution, think=False, options) and failure handling.
4. maybe_generate_and_save_title — DB + LLM mocked through monkeypatch;
   verifies the guard matrix (default title only, first user message only,
   no clobbering of renames that land mid-flight, disabled flag, exception
   swallowing).

No live database and no live Ollama are required — the session factory and
the conversations service are monkeypatched, following the same strategy as
the other test modules (see conftest.py).
"""

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import title_generator  # noqa: E402

CONV_ID = uuid.uuid4()


# ══════════════════════════════════════════════════════════════════════
# 1. _clean_title units
# ══════════════════════════════════════════════════════════════════════


def test_clean_title_plain():
    assert title_generator._clean_title("Photo Rename Script") == "Photo Rename Script"


def test_clean_title_strips_wrapping_quotes():
    assert title_generator._clean_title('"Sea Poem"') == "Sea Poem"
    assert title_generator._clean_title("‘Sea Poem’") == "Sea Poem"
    assert title_generator._clean_title("«Capitale du Maroc»") == "Capitale du Maroc"


def test_clean_title_strips_code_fences():
    assert title_generator._clean_title("```\nSea Poem\n```") == "Sea Poem"
    assert title_generator._clean_title("```text\nSea Poem\n```") == "Sea Poem"


def test_clean_title_strips_think_blocks():
    assert (
        title_generator._clean_title("<think>the user wants a poem</think>Sea Poem")
        == "Sea Poem"
    )


def test_clean_title_strips_unterminated_think():
    assert title_generator._clean_title("<think>reasoning forever") == ""


def test_clean_title_strips_label_prefix():
    assert title_generator._clean_title("Title: Sea Poem") == "Sea Poem"
    assert (
        title_generator._clean_title("Titre - Capitale du Maroc") == "Capitale du Maroc"
    )


def test_clean_title_takes_first_line_only():
    assert title_generator._clean_title("Sea Poem\n\nHope this helps!") == "Sea Poem"


def test_clean_title_strips_trailing_punctuation():
    assert title_generator._clean_title("Sea Poem.") == "Sea Poem"
    assert title_generator._clean_title("Capitale du Maroc ?") == "Capitale du Maroc"
    assert title_generator._clean_title("Recipe Ideas…") == "Recipe Ideas"


def test_clean_title_keeps_inner_punctuation():
    assert title_generator._clean_title("C# vs Python") == "C# vs Python"


def test_clean_title_caps_word_count():
    assert title_generator._clean_title("one two three four five six seven") == (
        "one two three four five six"
    )


def test_clean_title_caps_char_count():
    long_word = "a" * 100
    assert (
        len(title_generator._clean_title(long_word)) == title_generator.MAX_TITLE_CHARS
    )


def test_clean_title_collapses_whitespace():
    assert title_generator._clean_title("  Sea   \t Poem  ") == "Sea Poem"


def test_clean_title_empty_inputs():
    assert title_generator._clean_title("") == ""
    assert title_generator._clean_title("   ") == ""
    assert title_generator._clean_title("<think>only thinking</think>") == ""


def test_clean_title_fenced_with_label_and_quotes_together():
    """The full small-model disaster: think + fence + label + quotes + punct."""
    raw = '<think>user asks about morocco</think>\n```json\n"Title: Capitale du Maroc."\n```'
    assert title_generator._clean_title(raw) == "Capitale du Maroc"


# ══════════════════════════════════════════════════════════════════════
# 2. _fallback_title units
# ══════════════════════════════════════════════════════════════════════


def test_fallback_title_short_message_unchanged():
    msg = "help me debug a Flask app"
    assert title_generator._fallback_title(msg) == msg


def test_fallback_title_collapses_whitespace():
    assert title_generator._fallback_title("  hello   \n world  ") == "hello world"


def test_fallback_title_truncates_long_message():
    msg = "x" * 60
    result = title_generator._fallback_title(msg)
    assert result == "x" * 40 + "…"
    assert len(result) == 41


def test_fallback_title_empty():
    assert title_generator._fallback_title("") == ""
    assert title_generator._fallback_title("   ") == ""


# ══════════════════════════════════════════════════════════════════════
# 3. generate_title (httpx mocked)
# ══════════════════════════════════════════════════════════════════════


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        if self._payload.get("_raise"):
            raise RuntimeError("simulated HTTP error")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Records requests and replays a canned response (or exception)."""

    next_response = None
    requests = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _FakeAsyncClient.requests.append({"url": url, "json": json})
        resp = _FakeAsyncClient.next_response
        if isinstance(resp, BaseException):
            raise resp
        return resp


@pytest.fixture
def fake_httpx(monkeypatch):
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.next_response = _FakeResponse(
        {"message": {"content": "Photo Rename Script"}}
    )
    monkeypatch.setattr(
        title_generator, "httpx", SimpleNamespace(AsyncClient=_FakeAsyncClient)
    )
    return _FakeAsyncClient


def test_generate_title_success(fake_httpx):
    result = asyncio.run(
        title_generator.generate_title(
            "can you write a python script to rename photos?"
        )
    )
    assert result == "Photo Rename Script"


def test_generate_title_request_payload(fake_httpx):
    from app.config import settings

    asyncio.run(title_generator.generate_title("hello"))

    assert len(fake_httpx.requests) == 1
    req = fake_httpx.requests[0]
    # URL points at the configured Ollama base URL
    assert req["url"] == f"{settings.OLLAMA_BASE_URL}/api/chat"
    body = req["json"]
    # Model resolved through the utility role (cpu_small → qwen3:4b)
    assert body["model"] == settings.resolve_model(settings.TITLE_GENERATION_MODEL_ROLE)
    assert body["stream"] is False
    assert body["think"] is False
    assert body["options"]["num_predict"] == 30
    # System prompt + user message present
    assert body["messages"][0]["role"] == "system"
    assert "hello" in body["messages"][1]["content"]


def test_generate_title_truncates_long_message_in_prompt(fake_httpx):
    long_msg = "z" * 2000
    asyncio.run(title_generator.generate_title(long_msg))
    user_content = fake_httpx.requests[0]["json"]["messages"][1]["content"]
    assert len(user_content) <= title_generator._MAX_PROMPT_CHARS + 80


def test_generate_title_cleans_output(fake_httpx):
    fake_httpx.next_response = _FakeResponse({"message": {"content": '"Sea Poem"'}})
    result = asyncio.run(title_generator.generate_title("write a sea poem"))
    assert result == "Sea Poem"


def test_generate_title_unusable_output_returns_none(fake_httpx):
    fake_httpx.next_response = _FakeResponse({"message": {"content": "<think>x"}})
    assert asyncio.run(title_generator.generate_title("hi")) is None


def test_generate_title_http_error_returns_none(fake_httpx):
    fake_httpx.next_response = _FakeResponse({"_raise": True})
    assert asyncio.run(title_generator.generate_title("hi")) is None


def test_generate_title_connection_error_returns_none(fake_httpx):
    fake_httpx.next_response = RuntimeError("connection refused")
    assert asyncio.run(title_generator.generate_title("hi")) is None


# ══════════════════════════════════════════════════════════════════════
# 4. maybe_generate_and_save_title (DB + LLM mocked)
# ══════════════════════════════════════════════════════════════════════


class _FakeSession:
    def __init__(self):
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.commits += 1


class _ConvServiceHarness:
    """Monkeypatches conversations.get_conversation / get_messages /
    update_conversation_title with scriptable behavior."""

    def __init__(self, monkeypatch, conv_title="New Chat", user_messages=1):
        self.conv_title = conv_title
        self.user_messages = user_messages
        self.updates = []  # (conv_id, title) pairs passed to update
        self.get_conversation_calls = 0

        async def fake_get_conversation(db, conv_id):
            self.get_conversation_calls += 1
            if self.conv_title is None:
                return None
            return SimpleNamespace(id=conv_id, title=self.conv_title)

        async def fake_get_messages(db, conv_id, limit=100):
            msgs = [SimpleNamespace(role="user") for _ in range(self.user_messages)]
            # The assistant message is persisted BEFORE the title call in
            # the real flow — model that here.
            msgs.append(SimpleNamespace(role="assistant"))
            return msgs

        async def fake_update_title(db, conv_id, title):
            self.updates.append((conv_id, title))

        monkeypatch.setattr(
            title_generator.conv_service, "get_conversation", fake_get_conversation
        )
        monkeypatch.setattr(
            title_generator.conv_service, "get_messages", fake_get_messages
        )
        monkeypatch.setattr(
            title_generator.conv_service,
            "update_conversation_title",
            fake_update_title,
        )


@pytest.fixture
def db_harness(monkeypatch):
    """Session factory harness: fresh sessions, records commits."""

    class _Factory:
        def __init__(self):
            self.sessions = []

        def __call__(self):
            s = _FakeSession()
            self.sessions.append(s)
            return s

    factory = _Factory()
    monkeypatch.setattr(title_generator, "async_session_factory", factory)
    return factory


def _patch_llm(monkeypatch, title):
    async def fake_generate(user_message):
        return title

    monkeypatch.setattr(title_generator, "generate_title", fake_generate)


def test_maybe_titles_first_turn_and_persists(monkeypatch, db_harness):
    h = _ConvServiceHarness(monkeypatch, conv_title="New Chat", user_messages=1)
    _patch_llm(monkeypatch, "Flask Debugging")

    result = asyncio.run(
        title_generator.maybe_generate_and_save_title(CONV_ID, "help me debug Flask")
    )

    assert result == "Flask Debugging"
    assert h.updates == [(CONV_ID, "Flask Debugging")]
    # Exactly one session per phase (check phase + update phase), and the
    # update session committed.
    assert len(db_harness.sessions) == 2
    assert db_harness.sessions[1].commits == 1


def test_maybe_uses_fallback_when_llm_fails(monkeypatch, db_harness):
    h = _ConvServiceHarness(monkeypatch, conv_title="New Chat", user_messages=1)
    _patch_llm(monkeypatch, None)  # LLM failed

    result = asyncio.run(
        title_generator.maybe_generate_and_save_title(
            CONV_ID, "help me debug my Flask app please"
        )
    )
    # Fallback mirrors the 40-char truncation preview
    assert result == "help me debug my Flask app please"
    assert h.updates == [(CONV_ID, result)]


def test_maybe_skips_when_already_titled(monkeypatch, db_harness):
    h = _ConvServiceHarness(monkeypatch, conv_title="My Custom Name", user_messages=1)
    _patch_llm(monkeypatch, "Should Not Appear")

    result = asyncio.run(
        title_generator.maybe_generate_and_save_title(CONV_ID, "hello")
    )
    assert result is None
    assert h.updates == []
    assert h.get_conversation_calls == 1  # bailed after the first check


def test_maybe_skips_when_not_first_turn(monkeypatch, db_harness):
    h = _ConvServiceHarness(monkeypatch, conv_title="New Chat", user_messages=3)
    _patch_llm(monkeypatch, "Should Not Appear")

    result = asyncio.run(
        title_generator.maybe_generate_and_save_title(CONV_ID, "hello again")
    )
    assert result is None
    assert h.updates == []


def test_maybe_skips_when_conversation_missing(monkeypatch, db_harness):
    _ConvServiceHarness(monkeypatch, conv_title=None, user_messages=1)
    _patch_llm(monkeypatch, "Should Not Appear")

    result = asyncio.run(
        title_generator.maybe_generate_and_save_title(CONV_ID, "hello")
    )
    assert result is None


def test_maybe_skips_when_disabled(monkeypatch, db_harness):
    from app.config import settings

    h = _ConvServiceHarness(monkeypatch, conv_title="New Chat", user_messages=1)
    _patch_llm(monkeypatch, "Should Not Appear")
    monkeypatch.setattr(settings, "TITLE_GENERATION_ENABLED", False)

    result = asyncio.run(
        title_generator.maybe_generate_and_save_title(CONV_ID, "hello")
    )
    assert result is None
    assert h.updates == []


def test_maybe_does_not_clobber_rename_during_llm_call(monkeypatch, db_harness):
    """A manual rename landing while the LLM is thinking must win."""
    h = _ConvServiceHarness(monkeypatch, conv_title="New Chat", user_messages=1)
    _patch_llm(monkeypatch, "LLM Title")

    # First get_conversation (phase 1) sees the default title; the second
    # (phase 3, right before the write) sees a user-chosen title.
    titles = ["New Chat", "Renamed Mid-Flight"]

    async def fake_get_conversation(db, conv_id):
        h.get_conversation_calls += 1
        idx = min(h.get_conversation_calls - 1, len(titles) - 1)
        return SimpleNamespace(id=conv_id, title=titles[idx])

    monkeypatch.setattr(
        title_generator.conv_service, "get_conversation", fake_get_conversation
    )

    result = asyncio.run(
        title_generator.maybe_generate_and_save_title(CONV_ID, "hello")
    )
    assert result is None
    assert h.updates == []


def test_maybe_never_raises_on_db_error(monkeypatch, db_harness):
    _ConvServiceHarness(monkeypatch, conv_title="New Chat", user_messages=1)
    _patch_llm(monkeypatch, "LLM Title")

    class _ExplodingSession:
        async def __aenter__(self):
            raise RuntimeError("db exploded")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        title_generator, "async_session_factory", lambda: _ExplodingSession()
    )

    result = asyncio.run(
        title_generator.maybe_generate_and_save_title(CONV_ID, "hello")
    )
    assert result is None  # swallowed, never raised


def test_maybe_skips_empty_fallback(monkeypatch, db_harness):
    """Empty user message + failed LLM → nothing to title with."""
    h = _ConvServiceHarness(monkeypatch, conv_title="New Chat", user_messages=1)
    _patch_llm(monkeypatch, None)

    result = asyncio.run(title_generator.maybe_generate_and_save_title(CONV_ID, ""))
    assert result is None
    assert h.updates == []


def test_maybe_accepts_empty_string_title_as_default(monkeypatch, db_harness):
    """Conversations created with an empty title are still auto-titled."""
    h = _ConvServiceHarness(monkeypatch, conv_title="", user_messages=1)
    _patch_llm(monkeypatch, "Blank Start")

    result = asyncio.run(
        title_generator.maybe_generate_and_save_title(CONV_ID, "hello")
    )
    assert result == "Blank Start"
    assert h.updates == [(CONV_ID, "Blank Start")]


# ══════════════════════════════════════════════════════════════════════
# 5. Config / constants sanity
# ══════════════════════════════════════════════════════════════════════


def test_config_defaults():
    from app.config import settings

    assert settings.TITLE_GENERATION_ENABLED is True
    assert settings.TITLE_GENERATION_MODEL_ROLE == "default_utility"
    assert settings.TITLE_GENERATION_TIMEOUT_SECONDS == 60


def test_default_title_constant_matches():
    from app.services.conversations import DEFAULT_TITLE

    assert DEFAULT_TITLE == "New Chat"
    assert title_generator.DEFAULT_TITLE == DEFAULT_TITLE


def test_utility_role_resolves_in_profiles():
    """The default_utility role must resolve to a real model ID in every
    hardware profile (it's defined in profiles.yml for all of them)."""
    from app.config import settings

    resolved = settings.resolve_model(settings.TITLE_GENERATION_MODEL_ROLE)
    assert resolved and resolved != settings.TITLE_GENERATION_MODEL_ROLE
