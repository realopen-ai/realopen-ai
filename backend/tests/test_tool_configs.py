"""Tests for the Tools configuration system (Brain ▸ Tools).

Covers the full pipeline required by the task spec:
1. Seeding  — fresh DB seeds every discovered tool; existing rows are
   never overwritten; partial rows self-heal via read-time merge;
   startup DB failure falls back to code defaults (logged, not silent).
2. API      — GET /api/tools (all + single + 404), PUT config updates
   (universal + custom + validation errors), and the self-heal path
   for tools discovered after startup.
3. Semantics— disabled tools are excluded from the LLM tool
   selection / schema / system prompt; always_load + tag gating
   behavior integrates with the (formerly hardcoded) mechanism;
   legacy keyword_gate rows migrate to tags on read.
4. Model    — per-tool model override resolution, invalid override
   rejected at write time, and the agent loop actually switches
   models after a tool with an override executes (integration test
   with the provider stream and tool execution mocked).
5. Web Search — provider matrix read from the persisted config:
   toggling providers changes which search backends run; results are
   interleaved and labeled; failing providers are skipped. The
   provider implementations live in app/services/web_search.py
   (like the other services) — tests patch the service functions.
6. Secrets  — stored in the secret store (never in the config JSON),
   masked in API responses, validated before saving, cleared via null.
7. ArXiv    — the arXiv API is subject-based: search_query
   construction (cat: + ANDed keywords + quoted phrases), the
   ``arxiv`` package invocation, and subject validation.

The DB layer is a dict-backed fake session factory (same pattern as
test_documents_workspace_api) — no live Postgres needed.
"""

import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db import models  # noqa: E402
from app.agent.base import BaseTool, ToolResult, get_tool_registry  # noqa: E402
from app.agent import service  # noqa: E402
from app.agent.tools import config_store  # noqa: E402
from app.agent.tools.web_search import WebSearchTool  # noqa
from app.services import providers, secrets as secret_store  # noqa: E402
from app.services import web_search as web_search_service  # noqa: E402

# ─── Fakes: dict-backed DB session ─────────────────────────────────


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    """Async session over a dict of ToolConfig rows.

    Supports the two query shapes used by config_store:
    select(ToolConfig)                     → all rows
    select(ToolConfig).where(tool_name==X) → one row
    """

    def __init__(self, store: dict):
        self.store = store
        self._pending: list = []

    async def execute(self, stmt):
        rows = list(self.store.values())
        whereclause = getattr(stmt, "whereclause", None)
        if whereclause is not None:
            try:
                tool_name = whereclause.right.value
            except AttributeError:  # pragma: no cover - unexpected shape
                tool_name = None
            rows = [r for r in rows if r.tool_name == tool_name]
        return FakeResult(rows)

    def add(self, obj):
        self._pending.append(obj)

    async def commit(self):
        for obj in self._pending:
            self.store[obj.tool_name] = obj
        self._pending.clear()

    async def rollback(self):  # pragma: no cover - unused
        self._pending.clear()

    async def close(self):  # pragma: no cover - unused
        pass


class FakeSessionFactory:
    def __init__(self, store: dict, fail: bool = False):
        self.store = store
        self.fail = fail

    def __call__(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                if outer.fail:
                    raise ConnectionError("db down")
                return FakeSession(outer.store)

            async def __aexit__(self, *a):
                return False

        return _Ctx()


# ─── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect the persistent state dir (secrets, model prefs) to tmp."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr("app.config.settings._get_state_dir", lambda: state)
    # Deterministic model validation: Ollama reachable with one model.
    # (_fetch_ollama_tags returns a LIST of model dicts; the cache dict
    # mirrors the module-level shape.)

    async def fake_tags(force=False):
        return [{"id": "qwen3:8b", "installed": True}]

    monkeypatch.setattr(providers, "_fetch_ollama_tags", fake_tags)
    monkeypatch.setattr(
        providers,
        "_tags_cache",
        {
            "ts": 0.0,
            "models": [{"id": "qwen3:8b", "installed": True}],
            "reachable": True,
        },
    )
    return state


@pytest.fixture
def store():
    return {}


@pytest.fixture(autouse=True)
def reset_config_cache():
    """Isolate the config store cache between tests."""
    config_store._cache.clear()
    config_store._loaded = False
    yield
    config_store._cache.clear()
    config_store._loaded = False


@pytest.fixture
def db(store, monkeypatch):
    """Fake DB session factory installed for the test."""
    factory = FakeSessionFactory(store)
    monkeypatch.setattr("app.db.session.async_session_factory", factory)
    return store


@pytest.fixture
def client(db, tmp_state):
    """Seeded TestClient over an isolated app with the tools router."""
    import asyncio

    asyncio.run(config_store.seed_and_load())
    from app.api import tools as tools_api

    app = FastAPI()
    app.include_router(tools_api.router, prefix="/api")
    with TestClient(app) as c:
        yield c


# ─── 1. Seeding ────────────────────────────────────────────────────


class TestSeeding:
    @pytest.mark.asyncio
    async def test_fresh_database_seeds_every_discovered_tool(self, db):
        result = await config_store.seed_and_load()
        registry_names = {t.name for t in get_tool_registry().all_tools()}
        assert set(db.keys()) == registry_names
        assert result["seeded"] == len(registry_names)
        assert result["loaded"] == len(registry_names)

    @pytest.mark.asyncio
    async def test_seeded_defaults_mirror_previous_behavior(self, db):
        await config_store.seed_and_load()
        # websearch was an always-tool
        cfg = config_store.get_tool_config("use_websearch")
        assert cfg["enabled"] is True
        assert cfg["always_load"] is True
        # excel was tag-gated
        excel = config_store.get_tool_config("use_excel_gen")
        assert excel["always_load"] is False
        assert "excel" in excel["tags"]
        assert "spreadsheet" in excel["tags"]
        assert isinstance(excel["tags"], list)
        # model default = inherit
        assert cfg["model"] is None

    @pytest.mark.asyncio
    async def test_existing_configuration_not_overwritten(self, db):
        # Simulate an existing user configuration: websearch disabled
        db["use_websearch"] = models.ToolConfig(
            tool_name="use_websearch",
            config={
                "enabled": False,
                "always_load": False,
                "tags": ["weather"],
                "model": "qwen3:8b",
                "custom": {"providers": {"searxng": {"enabled": False}}},
            },
        )
        result = await config_store.seed_and_load()
        # Only the other tools were seeded; the row was preserved
        assert result["seeded"] == len(db) - 1
        cfg = config_store.get_tool_config("use_websearch")
        assert cfg["enabled"] is False
        assert cfg["tags"] == ["weather"]
        assert cfg["custom"]["providers"]["searxng"]["enabled"] is False
        assert cfg["model"] == "qwen3:8b"

    @pytest.mark.asyncio
    async def test_legacy_keyword_gate_row_migrates_to_tags(self, db):
        """A pre-tags row (keyword_gate string) is converted on read:
        the stored gate wins over the defaults, and the legacy key is
        hidden from every reader. Saving the row persists tags."""
        db["use_websearch"] = models.ToolConfig(
            tool_name="use_websearch",
            config={
                "enabled": True,
                "always_load": False,
                "keyword_gate": "weather, news, search",
                "custom": {},
            },
        )
        await config_store.seed_and_load()
        cfg = config_store.get_tool_config("use_websearch")
        assert cfg["tags"] == ["weather", "news", "search"]
        assert "keyword_gate" not in cfg
        # stored row is untouched (migration is read-time only)…
        assert db["use_websearch"].config["keyword_gate"] == "weather, news, search"
        # …but the API view already speaks tags
        view = await config_store.tool_api_view("use_websearch")
        assert view["config"]["tags"] == ["weather", "news", "search"]
        # saving ANY patch persists the migrated new-format row
        await config_store.update_tool_config("use_websearch", {"enabled": False})
        stored = db["use_websearch"].config
        assert "keyword_gate" not in stored
        assert stored["tags"] == ["weather", "news", "search"]
        assert stored["enabled"] is False

    @pytest.mark.asyncio
    async def test_legacy_keyword_gate_patch_accepted(self, db):
        """Old clients (comma string) can still update the gate — the
        value is normalized into the tags list."""
        await config_store.seed_and_load()
        await config_store.update_tool_config(
            "use_websearch", {"keyword_gate": "weather, news"}
        )
        cfg = config_store.get_tool_config("use_websearch")
        assert cfg["tags"] == ["weather", "news"]

    @pytest.mark.asyncio
    async def test_partial_config_self_heals_at_read_time(self, db):
        await config_store.seed_and_load()
        # A row with only one key: missing keys are filled from defaults
        # at READ time, without rewriting the stored row.
        db["use_websearch"].config = {"enabled": True}
        config_store._cache["use_websearch"] = {"enabled": True}
        cfg = config_store.get_tool_config("use_websearch")
        assert cfg["always_load"] is True  # default filled in
        assert cfg["custom"]["providers"]["searxng"]["enabled"] is True
        # stored row untouched
        assert db["use_websearch"].config == {"enabled": True}

    @pytest.mark.asyncio
    async def test_startup_db_failure_falls_back_to_defaults(
        self, tmp_state, monkeypatch
    ):
        monkeypatch.setattr(
            "app.db.session.async_session_factory", FakeSessionFactory({}, fail=True)
        )
        result = await config_store.seed_and_load()
        assert result["seeded"] == 0
        # The agent still works with code defaults
        cfg = config_store.get_tool_config("use_websearch")
        assert cfg is not None and cfg["enabled"] is True

    @pytest.mark.asyncio
    async def test_restart_keeps_database_configuration(self, db):
        """Restart simulation: seed → disable a tool → re-seed → still
        disabled (the DB is the source of truth after initialization)."""
        await config_store.seed_and_load()
        await config_store.update_tool_config("use_websearch", {"enabled": False})
        # Simulate a restart: fresh cache, same DB
        config_store._cache.clear()
        config_store._loaded = False
        await config_store.seed_and_load()
        assert config_store.get_tool_config("use_websearch")["enabled"] is False


# ─── 2. API ─────────────────────────────────────────────────────────


class TestApi:
    def test_list_tools_returns_every_discovered_tool(self, client):
        r = client.get("/api/tools")
        assert r.status_code == 200
        tools = r.json()["tools"]
        registry_names = {t.name for t in get_tool_registry().all_tools()}
        assert {t["tool"] for t in tools} == registry_names

    def test_list_tools_shape(self, client):
        r = client.get("/api/tools")
        tools = {t["tool"]: t for t in r.json()["tools"]}
        ws = tools["use_websearch"]
        assert ws["display_name"] == "Web Search"
        assert "search the web" in ws["description"].lower()
        assert ws["config"]["enabled"] is True
        assert ws["config"]["always_load"] is True
        assert ws["custom_schema"] is not None
        assert ws["has_custom"] is True
        # secret metadata present, but never a value
        assert "google_api_key" in ws["secrets"]
        assert ws["secrets"]["google_api_key"] == {"set": False}
        # basic tool: no custom config
        vision = tools["use_vision"]
        assert vision["display_name"] == "Vision"
        assert vision["has_custom"] is False

    def test_get_single_tool(self, client):
        r = client.get("/api/tools/use_websearch")
        assert r.status_code == 200
        assert r.json()["tool"] == "use_websearch"

    def test_get_unknown_tool_404(self, client):
        r = client.get("/api/tools/does_not_exist")
        assert r.status_code == 404

    def test_update_universal_settings(self, client, db):
        r = client.put(
            "/api/tools/use_websearch",
            json={"config": {"enabled": False, "tags": ["weather", "news"]}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["config"]["enabled"] is False
        assert body["config"]["tags"] == ["weather", "news"]
        assert body["default_tags"]  # frontend re-arm material
        # persisted to the (fake) DB
        assert db["use_websearch"].config["enabled"] is False
        # runtime cache updated (the agent reads this)
        assert config_store.get_tool_config("use_websearch")["enabled"] is False

    def test_update_tags_validation(self, client):
        # non-string entries rejected
        r = client.put("/api/tools/use_websearch", json={"config": {"tags": [42]}})
        assert r.status_code == 400
        # non-list, non-legacy-string types rejected
        r = client.put("/api/tools/use_websearch", json={"config": {"tags": True}})
        assert r.status_code == 400
        # comma string (legacy) accepted + normalized; deduped; blanks
        # dropped
        r = client.put(
            "/api/tools/use_websearch",
            json={"config": {"tags": "weather, weather , , news"}},
        )
        assert r.status_code == 200
        assert r.json()["config"]["tags"] == ["weather", "news"]
        # oversize tag rejected
        r = client.put(
            "/api/tools/use_websearch",
            json={"config": {"tags": ["x" * 61]}},
        )
        assert r.status_code == 400

    def test_update_partial_keeps_other_keys(self, client, db):
        r = client.put(
            "/api/tools/use_websearch", json={"config": {"always_load": False}}
        )
        assert r.status_code == 200
        cfg = db["use_websearch"].config
        assert cfg["always_load"] is False
        assert cfg["enabled"] is True  # untouched
        assert cfg["custom"]["providers"]["searxng"]["enabled"] is True

    def test_update_model_override_persisted(self, client, db):
        r = client.put(
            "/api/tools/use_websearch",
            json={"config": {"model": "qwen3:8b"}},
        )
        assert r.status_code == 200
        assert db["use_websearch"].config["model"] == "qwen3:8b"
        assert r.json()["config"]["model"] == "qwen3:8b"

    def test_update_model_reset_to_inherit(self, client, db):
        db["use_websearch"].config["model"] = "qwen3:8b"
        r = client.put("/api/tools/use_websearch", json={"config": {"model": None}})
        assert r.status_code == 200
        assert r.json()["config"]["model"] is None

    def test_update_invalid_model_rejected(self, client):
        r = client.put(
            "/api/tools/use_websearch",
            json={"config": {"model": "not-a-real-model"}},
        )
        assert r.status_code == 400
        assert "not installed" in r.json()["detail"]

    def test_update_invalid_types_rejected(self, client):
        for bad in (
            {"enabled": "yes"},
            {"always_load": 1},
            {"tags": 42},
            {"tags": [42]},
            {"model": 7},
        ):
            r = client.put("/api/tools/use_websearch", json={"config": bad})
            assert r.status_code == 400, bad

    def test_update_unknown_tool_404(self, client):
        r = client.put("/api/tools/nope", json={"config": {"enabled": True}})
        assert r.status_code == 404

    def test_update_custom_web_search_config(self, client, db):
        r = client.put(
            "/api/tools/use_websearch",
            json={
                "config": {
                    "custom": {
                        "providers": {
                            "arxiv": {"enabled": True, "max_results": 5},
                            "searxng": {"enabled": False},
                        }
                    }
                }
            },
        )
        assert r.status_code == 200
        cfg = db["use_websearch"].config
        assert cfg["custom"]["providers"]["arxiv"]["enabled"] is True
        assert cfg["custom"]["providers"]["searxng"]["enabled"] is False

    def test_update_custom_validation_errors(self, client):
        cases = [
            {"providers": {"unknown-provider": {"enabled": True}}},
            {"providers": {"arxiv": {"enabled": "yes"}}},
            {"providers": {"arxiv": {"max_results": -1}}},
            {"providers": {"arxiv": {"subject": "not a subject!"}}},
            {"providers": {"arxiv": {"subject": "CS.AI"}}},
            {"providers": {"arxiv": {"sort_by": "newest"}}},
            {"providers": {"searxng": {"base_url": "not-a-url"}}},
            {"providers": {"wikipedia": {"language": "not a code!"}}},
        ]
        for custom in cases:
            r = client.put(
                "/api/tools/use_websearch", json={"config": {"custom": custom}}
            )
            assert r.status_code == 400, custom

    def test_new_tool_registered_after_startup_is_seeded_on_read(self, client, db):
        """Self-heal: a tool discovered after startup gets its basic
        configuration without a restart or a central hardcoded list."""

        class FakeTool(BaseTool):
            name = "use_fake_future_tool"
            description = "A hypothetical tool added later."

            async def execute(self, **kwargs) -> ToolResult:  # pragma: no cover
                return ToolResult(success=True, output="ok")

        registry = get_tool_registry()
        registry.register(FakeTool())
        try:
            r = client.get("/api/tools")
            assert r.status_code == 200
            tools = {t["tool"] for t in r.json()["tools"]}
            assert "use_fake_future_tool" in tools
            # seeded with the universal basic config
            r2 = client.get("/api/tools/use_fake_future_tool")
            assert r2.status_code == 200
            cfg = r2.json()["config"]
            assert cfg["enabled"] is True
            assert "always_load" in cfg and "tags" in cfg
            assert "model" in cfg
            # persisted to the DB
            assert "use_fake_future_tool" in db
        finally:
            registry.unregister("use_fake_future_tool")


# ─── 3. Enabled / always_load / keyword gate semantics ─────────────


class TestSemantics:
    def test_disabled_tool_excluded_from_selection(self, client):
        client.put("/api/tools/use_websearch", json={"config": {"enabled": False}})
        selected = service._select_tools("search the web for current info")
        assert "use_websearch" not in selected

    @pytest.mark.asyncio
    async def test_disabled_tool_excluded_from_llm_schema_and_prompt(self, client):
        client.put("/api/tools/use_websearch", json={"config": {"enabled": False}})
        selected = service._select_tools("search the web for current info")
        schemas = await service._build_ollama_tools(selected)
        names = [s["function"]["name"] for s in schemas]
        assert "use_websearch" not in names
        prompt = await service._build_system_prompt(selected)
        assert "use_websearch" not in prompt

    def test_enabled_tool_included(self, client):
        client.put("/api/tools/use_websearch", json={"config": {"enabled": True}})
        selected = service._select_tools("search the web")
        assert "use_websearch" in selected

    def test_always_load_off_gates_tool(self, client):
        client.put(
            "/api/tools/use_websearch",
            json={"config": {"always_load": False, "tags": ["weather"]}},
        )
        assert "use_websearch" not in service._select_tools("hello there")
        assert "use_websearch" in service._select_tools("what's the weather")
        # multi-word tags work (like the original mechanism)
        client.put(
            "/api/tools/use_websearch",
            json={"config": {"tags": ["climate change"]}},
        )
        assert "use_websearch" in service._select_tools("effects of climate change")
        # tags match case-insensitively
        client.put(
            "/api/tools/use_websearch",
            json={"config": {"tags": ["Weather"]}},
        )
        assert "use_websearch" in service._select_tools("What's the WEATHER")

    def test_empty_tags_never_selects(self, client):
        client.put(
            "/api/tools/use_websearch",
            json={"config": {"always_load": False, "tags": []}},
        )
        assert "use_websearch" not in service._select_tools("weather search news")

    def test_defaults_preserve_original_behavior(self, client):
        """Fresh defaults select exactly like the pre-config system."""
        from app.agent.tools.tool_defaults import _ALWAYS_TOOLS, _KEYWORD_TOOLS

        for msg in (
            "hello there",
            "search the web for current info",
            "make me an excel spreadsheet",
            "look at this image",
            "remember what we discussed last week",
        ):
            expected = set(_ALWAYS_TOOLS)
            lower = msg.lower()
            for kw, tools in _KEYWORD_TOOLS.items():
                if kw in lower:
                    expected.update(tools)
            assert service._select_tools(msg) == expected, msg


# ─── 4. Model override ─────────────────────────────────────────────


class TestModelOverride:
    @pytest.mark.asyncio
    async def test_override_respected_in_resolution(self, client):
        client.put("/api/tools/use_websearch", json={"config": {"model": "qwen3:8b"}})
        assert await config_store.tool_model_override("use_websearch") == "qwen3:8b"

    @pytest.mark.asyncio
    async def test_invalid_override_rejected_at_write_time(self, client):
        # A model that is NOT installed (fake tags only know qwen3:8b)
        r = client.put(
            "/api/tools/use_vision", json={"config": {"model": "bogus-model"}}
        )
        assert r.status_code == 400
        # Nothing was written → resolution inherits the task slot.
        assert await config_store.tool_model_override("use_vision") is None

    @pytest.mark.asyncio
    async def test_resolve_tool_model_prefers_override_over_task_slot(self, client):
        client.put("/api/tools/use_websearch", json={"config": {"model": "qwen3:8b"}})
        resolved = await config_store.resolve_tool_model("use_websearch", task="chat")
        assert resolved == "qwen3:8b"

    @pytest.mark.asyncio
    async def test_resolve_tool_model_falls_back_to_task_slot(self, client):
        resolved = await config_store.resolve_tool_model("use_vision", task="vision")
        assert resolved  # some concrete model id from the vision slot

    @pytest.mark.asyncio
    async def test_agent_loop_switches_model_after_tool_with_override(self, client):
        """Integration: the model that processes a tool's results is the
        tool's override — verified end-to-end with a mocked provider
        stream and a mocked web search execution."""
        import json as jsonlib

        client.put("/api/tools/use_websearch", json={"config": {"model": "qwen3:8b"}})

        calls = []

        async def fake_stream_chat(model, messages, tools=None, timeout=0.0, **kw):
            calls.append({"model": model, "tools": tools})
            if len(calls) == 1:
                yield {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "use_websearch",
                                "arguments": jsonlib.dumps({"query": "test"}),
                            }
                        }
                    ],
                    "done": True,
                }
            else:
                yield {"content": "All done.", "done": True}

        async def fake_websearch(**kwargs):
            return ToolResult(success=True, output="Web search results for test")

        mp = pytest.MonkeyPatch()
        mp.setattr(providers, "stream_chat", fake_stream_chat)
        websearch_tool = get_tool_registry().get("use_websearch")
        mp.setattr(websearch_tool, "execute", fake_websearch)
        # No memories / cross-session DB access (raises → caught)
        mp.setattr(service, "async_session_factory", FakeSessionFactory({}, fail=True))
        try:
            events = [  # noqa
                e
                async for e in service.run_agent_stream(
                    [{"role": "user", "content": "search the web"}],
                    model="default",
                )
            ]
        finally:
            mp.undo()

        # Two provider rounds: the second (post-tool) uses the override
        assert len(calls) >= 2
        assert calls[0]["model"] != "qwen3:8b"
        assert calls[1]["model"] == "qwen3:8b"
        # Tool schema of the first round contains websearch (enabled)
        first_tools = calls[0]["tools"] or []
        names = [t["function"]["name"] for t in first_tools]
        assert "use_websearch" in names

    @pytest.mark.asyncio
    async def test_agent_loop_excludes_disabled_tool_from_schema(self, client):
        """End-to-end: disabling a tool removes it from the tools param
        actually sent to the LLM provider."""
        client.put("/api/tools/use_code_exec", json={"config": {"enabled": False}})

        calls = []

        async def fake_stream_chat(model, messages, tools=None, timeout=0.0, **kw):
            calls.append({"model": model, "tools": tools})
            yield {"content": "done", "done": True}

        mp = pytest.MonkeyPatch()
        mp.setattr(providers, "stream_chat", fake_stream_chat)
        mp.setattr(service, "async_session_factory", FakeSessionFactory({}, fail=True))
        try:
            async for _ in service.run_agent_stream(
                [{"role": "user", "content": "calculate 2+2 and run some code"}],
                model="qwen3:4b",
            ):
                pass
        finally:
            mp.undo()

        assert calls, "provider was never called"
        names = [t["function"]["name"] for t in (calls[0]["tools"] or [])]
        assert "use_code_exec" not in names


# ─── 5. Web Search provider matrix ─────────────────────────────────


class TestWebSearchProviders:
    @pytest.fixture
    def websearch_tool(self):
        return get_tool_registry().get("use_websearch")

    def _set_providers(self, client, providers_cfg):
        r = client.put(
            "/api/tools/use_websearch",
            json={"config": {"custom": {"providers": providers_cfg}}},
        )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_default_uses_searxng_only(self, client, websearch_tool, monkeypatch):
        called = {}

        async def fake_searxng(query, **kw):
            called["searxng"] = query
            return (
                "searxng",
                [
                    {
                        "provider": "SearXNG",
                        "title": "Result",
                        "url": "http://r",
                        "snippet": "s",
                    }
                ],
            )

        async def must_not_run(query, **kw):  # pragma: no cover
            raise AssertionError("must not run")

        monkeypatch.setattr(web_search_service, "search_searxng", fake_searxng)
        monkeypatch.setattr(web_search_service, "search_arxiv", must_not_run)
        monkeypatch.setattr(web_search_service, "search_wikipedia", must_not_run)
        monkeypatch.setattr(web_search_service, "search_google", must_not_run)

        result = await websearch_tool.execute(query="hello")
        assert result.success
        assert called["searxng"] == "hello"
        assert result.tool_call.web_results[0]["provider"] == "SearXNG"

    @pytest.mark.asyncio
    async def test_provider_toggle_changes_runtime_behavior(
        self, client, websearch_tool, monkeypatch
    ):
        """Disabling SearXNG + enabling ArXiv/Wikipedia actually switches
        which search backends run (the DB config is the source of truth)."""
        self._set_providers(
            client,
            {
                "searxng": {"enabled": False},
                "arxiv": {"enabled": True, "max_results": 2},
                "wikipedia": {"enabled": True, "max_results": 2},
            },
        )

        async def must_not_run(query, **kw):  # pragma: no cover
            raise AssertionError("searxng must not run")

        async def fake_arxiv(query, subject="", max_results=3, sort_by="relevance"):
            return (
                "arxiv",
                [
                    {
                        "provider": "ArXiv",
                        "title": f"Paper on {query}",
                        "url": "http://arxiv/1",
                        "snippet": "abstract",
                    }
                ],
            )

        async def fake_wikipedia(query, language="en", max_results=3):
            return (
                "wikipedia",
                [
                    {
                        "provider": "Wikipedia",
                        "title": f"{query} (Wikipedia)",
                        "url": "http://wiki/1",
                        "snippet": "intro",
                    }
                ],
            )

        monkeypatch.setattr(web_search_service, "search_searxng", must_not_run)
        monkeypatch.setattr(web_search_service, "search_arxiv", fake_arxiv)
        monkeypatch.setattr(web_search_service, "search_wikipedia", fake_wikipedia)

        result = await websearch_tool.execute(query="transformers")
        assert result.success
        providers_seen = {r["provider"] for r in result.tool_call.web_results}
        assert providers_seen == {"ArXiv", "Wikipedia"}
        assert "SearXNG" not in result.output
        assert "ArXiv" in result.output and "Wikipedia" in result.output

    @pytest.mark.asyncio
    async def test_failing_provider_is_skipped(
        self, client, websearch_tool, monkeypatch
    ):
        # SearXNG enabled (will fail) + Wikipedia enabled (works)
        self._set_providers(client, {"wikipedia": {"enabled": True, "max_results": 2}})

        async def failing(query, **kw):
            raise RuntimeError("provider down")

        async def ok_wikipedia(query, language="en", max_results=3):
            return (
                "wikipedia",
                [
                    {
                        "provider": "Wikipedia",
                        "title": "t",
                        "url": "http://w",
                        "snippet": "s",
                    }
                ],
            )

        monkeypatch.setattr(web_search_service, "search_searxng", failing)
        monkeypatch.setattr(web_search_service, "search_wikipedia", ok_wikipedia)

        result = await websearch_tool.execute(query="hello")
        assert result.success
        assert [r["provider"] for r in result.tool_call.web_results] == ["Wikipedia"]

    @pytest.mark.asyncio
    async def test_no_providers_enabled_returns_error(self, client, websearch_tool):
        self._set_providers(
            client,
            {
                "searxng": {"enabled": False},
                "arxiv": {"enabled": False},
                "wikipedia": {"enabled": False},
                "google": {"enabled": False},
            },
        )
        result = await websearch_tool.execute(query="hello")
        assert not result.success
        assert "no active providers" in result.output.lower()

    @pytest.mark.asyncio
    async def test_searxng_base_url_override(self, client, websearch_tool, monkeypatch):
        captured = {}

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"results": []}

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None):
                captured["url"] = url
                return R()

        monkeypatch.setattr("app.services.web_search.httpx.AsyncClient", FakeClient)
        self._set_providers(
            client, {"searxng": {"base_url": "http://custom-searx:9000"}}
        )
        await websearch_tool.execute(query="hello")
        assert captured["url"] == "http://custom-searx:9000/search"

    def test_interleave_round_robin(self):
        merged = web_search_service.interleave(
            {
                "a": [{"i": 1}, {"i": 3}],
                "b": [{"i": 2}, {"i": 4}],
            }
        )
        assert [m["i"] for m in merged] == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_google_without_key_fails_gracefully(self, client, websearch_tool):
        """Google enabled without key/cse → provider fails (skipped);
        with no other provider the tool returns a clear error."""
        self._set_providers(
            client,
            {
                "searxng": {"enabled": False},
                "google": {"enabled": True, "cse_id": ""},
            },
        )
        result = await websearch_tool.execute(query="hello")
        assert not result.success

    @pytest.mark.asyncio
    async def test_google_uses_secret_and_cse_id(
        self, client, websearch_tool, monkeypatch, tmp_state
    ):
        """With the key in the secret store + cse_id in the config, the
        Google provider actually queries the Custom Search API."""
        self._set_providers(
            client,
            {"google": {"enabled": True, "cse_id": "abc:xyz", "max_results": 3}},
        )
        secret_store.set_secret("use_websearch", "google_api_key", "AIzaTestKey123456")

        captured = {}

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "items": [
                        {
                            "title": "Google hit",
                            "link": "http://g/1",
                            "snippet": "from google",
                        }
                    ]
                }

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None):
                captured["url"] = url
                captured["params"] = params
                return R()

        monkeypatch.setattr("app.services.web_search.httpx.AsyncClient", FakeClient)
        result = await websearch_tool.execute(query="hello")
        assert result.success
        assert captured["url"] == "https://www.googleapis.com/customsearch/v1"
        assert captured["params"]["key"] == "AIzaTestKey123456"
        assert captured["params"]["cx"] == "abc:xyz"
        assert result.tool_call.web_results[0]["provider"] == "Google"


# ─── 5c. Schema contract: field keys resolve into the stored custom ─


class TestSchemaContract:
    def test_custom_schema_keys_resolve_into_custom_defaults(self):
        """Every custom_schema field key must be a real path into the
        stored custom object — the generic renderer writes there."""
        from app.agent.tools.web_search import WEB_SEARCH_CONFIG

        defaults = WEB_SEARCH_CONFIG.custom_defaults

        def resolve(obj, dotted):
            for part in dotted.split("."):
                assert part in obj, f"missing key {dotted!r} ({part!r})"
                obj = obj[part]
            return obj

        for section in WEB_SEARCH_CONFIG.custom_schema:
            for field in section["fields"]:
                resolve(defaults, field["key"])

    def test_schema_field_keys_are_unique(self):
        from app.agent.tools.web_search import WEB_SEARCH_CONFIG

        keys = [f["key"] for s in WEB_SEARCH_CONFIG.custom_schema for f in s["fields"]]
        assert len(keys) == len(set(keys))
        # fully-qualified (providers.<provider>.<setting>)
        for key in keys:
            assert key.startswith("providers."), key


# ─── 5b. ArXiv: subject-based query construction ──────────────────


class TestArxivQueryConstruction:
    """The arXiv API is a field/subject search, not a whole-sentence
    search (per the API user manual) — these tests pin the
    translation the service performs."""

    def test_whole_sentence_becomes_anded_keywords(self):
        q = web_search_service.build_arxiv_query(
            "latest research on transformer models for image classification"
        )
        assert q == (
            "all:transformer AND all:models AND all:image " "AND all:classification"
        )

    def test_subject_prepends_category(self):
        q = web_search_service.build_arxiv_query(" diffusion models ", "cs.CV")
        assert q == "cat:cs.CV AND all:diffusion AND all:models"

    def test_subject_only_browses_the_category(self):
        assert web_search_service.build_arxiv_query("", "cs.AI") == "cat:cs.AI"
        # a purely-noise query also degrades to the subject
        assert (
            web_search_service.build_arxiv_query(
                "find me the latest research papers please", "stat.ML"
            )
            == "cat:stat.ML"
        )

    def test_quoted_query_is_phrase_search(self):
        assert (
            web_search_service.build_arxiv_query('"attention is all you need"')
            == 'all:"attention is all you need"'
        )

    def test_subject_plus_phrase(self):
        assert (
            web_search_service.build_arxiv_query('"graph neural networks"', "cs.LG")
            == 'cat:cs.LG AND all:"graph neural networks"'
        )

    def test_no_subject_no_keywords_is_empty(self):
        assert web_search_service.build_arxiv_query("the of and") == ""
        assert web_search_service.build_arxiv_query("   ") == ""

    def test_keywords_capped_and_deduplicated(self):
        kws = web_search_service.arxiv_keywords(
            "quantum quantum computing entanglement error correction "
            "codes surface codes thresholds noise"
        )
        assert len(kws) == 5
        assert len(set(kws)) == 5
        assert "quantum" in kws

    def test_subject_regex(self):
        ok = [
            "cs.AI",
            "cs.CL",
            "stat.ML",
            "quant-ph",
            "cond-mat",
            "cond-mat.mes-hall",
            "physics.app-ph",
            "eess.AS",
            "math.ST",
        ]
        for s in ok:
            assert web_search_service.ARXIV_SUBJECT_RE.match(s), s
        bad = [
            "CS.AI",
            "cs..AI",
            "not a subject",
            "cs.AI OR cs.CL",
            "cs.AI AND all:x",
            "/etc/passwd",
        ]
        for s in bad:
            assert not web_search_service.ARXIV_SUBJECT_RE.match(s), s

    @pytest.mark.asyncio
    async def test_arxiv_provider_runs_package_in_thread(self, monkeypatch):
        """search_arxiv builds the query, maps the sort name, and runs
        the (blocking) package call via to_thread."""
        captured = {}

        class FakeResult:
            def __init__(
                self, title, entry_id, summary, authors, published, categories, pdf_url
            ):
                self.title = title
                self.entry_id = entry_id
                self.summary = summary
                self.authors = [type("A", (), {"name": n}) for n in authors]
                self.published = published
                self.categories = categories
                self.pdf_url = pdf_url

        def fake_run(search_query, max_results, sort_by):
            captured["query"] = search_query
            captured["max_results"] = max_results
            captured["sort_by"] = sort_by
            return []  # shape verified via a real parse below

        monkeypatch.setattr(web_search_service, "_run_arxiv_search", fake_run)
        outcome = await web_search_service.search_arxiv(
            "image classification",
            subject="cs.CV",
            max_results=4,
            sort_by="submittedDate",
        )
        assert outcome[0] == "arxiv"
        assert captured["query"] == "cat:cs.CV AND all:image AND all:classification"
        assert captured["max_results"] == 4
        assert captured["sort_by"] == "submittedDate"

    @pytest.mark.asyncio
    async def test_arxiv_subject_only_defaults_to_newest(self, monkeypatch):
        captured = {}

        def fake_run(search_query, max_results, sort_by):
            captured["sort_by"] = sort_by
            return []

        monkeypatch.setattr(web_search_service, "_run_arxiv_search", fake_run)
        await web_search_service.search_arxiv("latest research", subject="cs.AI")
        # relevance is undefined for a bare cat: browse → newest first
        assert captured["sort_by"] == "submittedDate"

    @pytest.mark.asyncio
    async def test_arxiv_empty_query_errors(self):
        with pytest.raises(RuntimeError):
            await web_search_service.search_arxiv("the of and")

    @pytest.mark.asyncio
    async def test_run_arxiv_search_maps_result_fields(self, monkeypatch):
        """The blocking runner feeds the arxiv package and maps Result
        objects into the standard result dict shape."""
        import datetime

        class FakeAuthor:
            def __init__(self, name):
                self.name = name

        class FakeResult:
            title = "A  Study  of\nAttention"
            entry_id = "http://arxiv.org/abs/1706.03762v7"
            summary = "  We  propose  the Transformer.  "
            authors = [FakeAuthor("A. Vaswani"), FakeAuthor("N. Shazeer")]
            published = datetime.datetime(2017, 6, 12)
            categories = ["cs.CL", "cs.LG"]
            pdf_url = "http://arxiv.org/pdf/1706.03762v7"

        class FakeSearch:
            def __init__(self, query, max_results, sort_by):
                self.query = query
                self.max_results = max_results
                self.sort_by = sort_by

        class FakeClient:
            def __init__(self, page_size, delay_seconds, num_retries):
                pass

            def results(self, search):
                captured["search"] = search
                return iter([FakeResult()])

        import arxiv as arxiv_pkg

        captured = {}
        monkeypatch.setattr(arxiv_pkg, "Search", FakeSearch)
        monkeypatch.setattr(arxiv_pkg, "Client", FakeClient)
        out = web_search_service._run_arxiv_search(
            "cat:cs.CL AND all:attention", 5, "relevance"
        )
        assert captured["search"].query == "cat:cs.CL AND all:attention"
        assert captured["search"].max_results == 5
        assert len(out) == 1
        r = out[0]
        assert r["provider"] == "ArXiv"
        assert r["title"] == "A Study of Attention"
        assert r["url"] == "http://arxiv.org/abs/1706.03762v7"
        assert r["pdf_url"] == "http://arxiv.org/pdf/1706.03762v7"
        assert r["categories"] == ["cs.CL", "cs.LG"]
        assert r["snippet"].startswith("[2017-06] A. Vaswani, N. Shazeer.")
        assert "We propose the Transformer." in r["snippet"]

    @pytest.mark.asyncio
    async def test_service_search_with_explicit_provider_cfg(self):
        """The service entry point accepts an explicit provider matrix
        (used by tests) and honors arxiv subject settings."""
        calls = {}

        async def fake_arxiv(query, subject="", max_results=5, sort_by="relevance"):
            calls["subject"] = subject
            return (
                "arxiv",
                [{"provider": "ArXiv", "title": "t", "url": "u", "snippet": "s"}],
            )

        async def must_not_run(*a, **kw):  # pragma: no cover
            raise AssertionError("searxng must not run")

        import unittest.mock as mock

        with mock.patch.object(
            web_search_service, "search_arxiv", fake_arxiv
        ), mock.patch.object(web_search_service, "search_searxng", must_not_run):
            outcome = await web_search_service.search(
                "image classification",
                providers_cfg={
                    "searxng": {"enabled": False},
                    "arxiv": {"enabled": True, "subject": "cs.CV"},
                },
            )
        assert calls["subject"] == "cs.CV"
        assert outcome["no_providers"] is False
        assert [r["provider"] for r in outcome["results"]] == ["ArXiv"]


# ─── 6. Secrets ─────────────────────────────────────────────────────


class TestSecrets:
    def test_secret_store_roundtrip(self, tmp_state):
        assert secret_store.get_secret("use_websearch", "google_api_key") is None
        assert secret_store.set_secret(
            "use_websearch", "google_api_key", "AIzaSyD-1234567890abcdef"
        )
        assert (
            secret_store.get_secret("use_websearch", "google_api_key")
            == "AIzaSyD-1234567890abcdef"
        )
        status = secret_store.secret_status("use_websearch", "google_api_key")
        assert status["set"] is True
        assert "•" in status["masked"]
        assert "AIzaSyD" not in status["masked"]
        # stored with 0600 permissions
        path = tmp_state / "tool_secrets" / "use_websearch" / "google_api_key"
        assert path.exists()
        assert (path.stat().st_mode & 0o777) == 0o600
        assert secret_store.clear_secret("use_websearch", "google_api_key")
        assert secret_store.get_secret("use_websearch", "google_api_key") is None

    def test_unsafe_secret_names_rejected(self, tmp_state):
        assert secret_store.set_secret("../evil", "field", "x") is False
        assert secret_store.set_secret("tool", "../escape", "x") is False

    def test_api_secret_flow(self, client, db, tmp_state, monkeypatch):
        # Make the Google key validator accept offline (no real network)
        from app.agent.tools import web_search as ws_module

        async def accept(value):
            return True, "unverified"

        monkeypatch.setitem(ws_module.WEB_SEARCH_CONFIG.secrets[0], "validate", accept)
        r = client.put(
            "/api/tools/use_websearch",
            json={
                "config": {
                    "custom": {
                        "providers": {"google": {"enabled": True, "cse_id": "abc:xyz"}}
                    }
                },
                "secrets": {"google_api_key": "AIzaSyD-1234567890abcdef"},
            },
        )
        assert r.status_code == 200, r.text
        # The secret is in the file store, NOT in the config JSON
        assert (
            secret_store.get_secret("use_websearch", "google_api_key")
            == "AIzaSyD-1234567890abcdef"
        )
        assert "google_api_key" not in str(db["use_websearch"].config)
        assert "AIzaSyD" not in str(db["use_websearch"].config)
        # API responses show only the masked status
        view = client.get("/api/tools/use_websearch").json()
        assert view["secrets"]["google_api_key"]["set"] is True
        assert "AIzaSyD" not in str(view)

    def test_secret_validation_rejection(self, client, tmp_state, monkeypatch):
        from app.agent.tools import web_search as ws_module

        async def reject(value):
            return False, "Invalid key — Google rejected it"

        monkeypatch.setitem(ws_module.WEB_SEARCH_CONFIG.secrets[0], "validate", reject)
        r = client.put(
            "/api/tools/use_websearch",
            json={
                "config": {"custom": {"providers": {"google": {"cse_id": "abc"}}}},
                "secrets": {"google_api_key": "AIzaBAD"},
            },
        )
        assert r.status_code == 400
        assert "rejected" in r.json()["detail"].lower()
        assert secret_store.get_secret("use_websearch", "google_api_key") is None

    @pytest.mark.asyncio
    async def test_real_google_validator_offline_accepted(self, monkeypatch, client):
        """The real validator treats network-unreachable as accepted
        (the app is offline-first), and 403 as rejected."""
        from app.agent.tools import web_search as ws_module

        # config with cse_id so validation can run
        client.put(
            "/api/tools/use_websearch",
            json={"config": {"custom": {"providers": {"google": {"cse_id": "abc"}}}}},
        )

        class OfflineClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None):
                raise httpx.ConnectError("no network")

        monkeypatch.setattr(ws_module.httpx, "AsyncClient", OfflineClient)
        ok, msg = await ws_module.validate_google_secret("AIzaSomething")
        assert ok is True

        class DeniedClient(OfflineClient):
            async def get(self, url, params=None):
                class Resp:
                    status_code = 403
                    text = '{"error": {"message": "bad key"}}'

                return Resp()

        monkeypatch.setattr(ws_module.httpx, "AsyncClient", DeniedClient)
        ok, msg = await ws_module.validate_google_secret("AIzaSomething")
        assert ok is False
        assert "bad key" in msg

    def test_secret_clear_via_null(self, client, tmp_state):
        secret_store.set_secret("use_websearch", "google_api_key", "AIzaXYZ123456")
        r = client.put(
            "/api/tools/use_websearch",
            json={"secrets": {"google_api_key": None}},
        )
        assert r.status_code == 200
        assert secret_store.get_secret("use_websearch", "google_api_key") is None

    def test_unknown_secret_field_rejected(self, client):
        r = client.put(
            "/api/tools/use_websearch",
            json={"secrets": {"not_a_field": "x"}},
        )
        assert r.status_code == 400
