/**
 * Tests for src/api/client.ts — the core REST client (models, providers,
 * modules, conversations).
 *
 * Direct import requires the alias hooks (`@/lib/debug`, plus type-only
 * `@/` imports — see tests/helpers/viteCompat.ts); every endpoint is served
 * by the globalThis.fetch mock. URL construction, request bodies, unwrapping
 * (`data.x ?? fallback`), and the never-throw contract of the soft-failing
 * helpers are asserted against real Response objects.
 */
import assert from "node:assert/strict";
import test from "node:test";

import "./helpers/viteCompat.ts";
import {
  installFetch,
  jsonResponse,
  sseData,
  sseResponse,
} from "./helpers/fetchMock.ts";

const client = await import("../src/api/client.ts");

test("fetchModels() returns the backend list and falls back when unreachable", async () => {
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/profile/models");
    assert.equal(init, undefined);
    throw new TypeError("fetch failed");
  });
  try {
    // Both a network error and a non-OK response fall back to the curated
    // offline list instead of throwing.
    const fallback = await client.fetchModels();
    assert.equal(fallback.profile, "cpu_small");
    assert.deepEqual(
      fallback.models.map((m) => m.id),
      ["qwen3:4b", "moondream:1.8b", "nomic-embed-text:v1.5"],
    );
  } finally {
    mock.restore();
  }

  const mock2 = installFetch(() =>
    jsonResponse(
      {
        models: [
          {
            id: "llama3:8b",
            type: "chat",
            role: "default",
            description: "",
            size: "4 GB",
          },
        ],
        profile: "cpu_large",
        label: "CPU",
      },
      200,
    ),
  );
  try {
    const live = await client.fetchModels();
    assert.equal(live.profile, "cpu_large");
    assert.equal(live.models[0].id, "llama3:8b");
  } finally {
    mock2.restore();
  }
});

test("fetchAvailableModels() resolves null instead of throwing on failure", async () => {
  const mock = installFetch(() => jsonResponse({}, 500));
  try {
    assert.equal(await client.fetchAvailableModels(), null);
  } finally {
    mock.restore();
  }
});

test("setModelPreference() PUTs {task, model} and resolves null on rejection", async () => {
  let ok = true;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/models/preferences");
    assert.equal(init?.method, "PUT");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      task: "vision",
      model: "moondream:1.8b",
    });
    return ok
      ? jsonResponse({
          task: "vision",
          model: "moondream:1.8b",
          is_default: false,
        })
      : jsonResponse({ detail: "nope" }, 422);
  });
  try {
    const row = await client.setModelPreference("vision", "moondream:1.8b");
    assert.equal(row?.model, "moondream:1.8b");

    ok = false;
    assert.equal(await client.setModelPreference("vision", null), null);
  } finally {
    mock.restore();
  }
});

test("voice settings endpoints throw on failure and send patches on save", async () => {
  const mock = installFetch((url, init) => {
    if (url === "/api/voice/settings" && !init) {
      return jsonResponse({
        voice: "alloy",
        speed: 1,
        persona: "p",
        custom_personas: [],
        personas: {},
      });
    }
    if (url === "/api/voice/settings" && init?.method === "PUT") {
      assert.deepEqual(JSON.parse(String(init?.body)), { speed: 1.5 });
      return jsonResponse({});
    }
    if (url === "/api/voice/voices" && !init) {
      return jsonResponse({ builtin: ["alloy"], custom: [] });
    }
    throw new Error(`Unexpected fetch in test: ${url}`);
  });
  try {
    assert.equal((await client.fetchVoiceSettings()).voice, "alloy");
    await client.updateVoiceSettings({ speed: 1.5 });
    assert.deepEqual(await client.fetchVoices(), {
      builtin: ["alloy"],
      custom: [],
    });
  } finally {
    mock.restore();
  }

  const failing = installFetch(() => jsonResponse({}, 500));
  try {
    await assert.rejects(
      () => client.fetchVoiceSettings(),
      /Could not load voice settings/,
    );
    await assert.rejects(
      () => client.updateVoiceSettings({}),
      /Could not save voice settings/,
    );
    await assert.rejects(() => client.fetchVoices(), /Could not load voices/);
  } finally {
    failing.restore();
  }
});

test("uploadVoice() posts multipart and surfaces the backend detail", async () => {
  let status = 200;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/voice/voices");
    assert.equal(init?.method, "POST");
    assert.ok(init?.body instanceof FormData);
    const file = (init?.body as FormData).get("file");
    assert.equal((file as File).name, "voice.wav");
    if (status === 200)
      return jsonResponse({ id: "v1", name: "Voice", voice: "v1" });
    return jsonResponse(
      status === 422 ? { detail: "Unsupported codec" } : {},
      status,
    );
  });
  try {
    const file = new File([new Uint8Array([1, 2, 3])], "voice.wav");
    assert.equal((await client.uploadVoice(file)).id, "v1");

    status = 422;
    await assert.rejects(() => client.uploadVoice(file), /Unsupported codec/);

    status = 500;
    await assert.rejects(() => client.uploadVoice(file), /Voice import failed/);
  } finally {
    mock.restore();
  }
});

test("provider endpoints unwrap, validate, and never throw", async () => {
  const groqRow = {
    id: "groq",
    name: "Groq",
    kind: "cloud" as const,
    connected: true,
    model_count: 5,
  };
  let scenario = "providers-ok";
  const mock = installFetch((url, init) => {
    if (scenario === "throw") throw new TypeError("fetch failed");
    switch (scenario) {
      case "providers-ok":
        assert.equal(url, "/api/providers");
        return jsonResponse({ providers: [groqRow] });
      case "providers-empty":
        return jsonResponse({});
      case "groq-connect":
        assert.deepEqual(JSON.parse(String(init?.body)), { api_key: "gsk_1" });
        return jsonResponse({ key_masked: "gsk_…1" });
      case "groq-invalid":
        return jsonResponse({ detail: "Invalid key" }, 401);
      case "groq-no-detail":
        return jsonResponse({}, 503);
      case "groq-disconnect-ok":
        assert.equal(init?.method, "DELETE");
        return jsonResponse({});
      default:
        throw new Error(`Unexpected scenario: ${scenario}`);
    }
  });
  try {
    assert.deepEqual(await client.fetchProviders(), [groqRow]);

    scenario = "groq-connect";
    assert.deepEqual(await client.connectGroqProvider("gsk_1"), {
      ok: true,
      keyMasked: "gsk_…1",
    });

    scenario = "groq-invalid";
    assert.deepEqual(await client.connectGroqProvider("bad"), {
      ok: false,
      error: "Invalid key",
    });

    scenario = "groq-no-detail";
    assert.deepEqual(await client.connectGroqProvider("bad"), {
      ok: false,
      error: "Request failed (503)",
    });

    scenario = "groq-disconnect-ok";
    assert.equal(await client.disconnectGroqProvider(), true);

    scenario = "throw";
    assert.deepEqual(await client.fetchProviders(), []);
    assert.deepEqual(await client.connectGroqProvider("x"), {
      ok: false,
      error: "Cannot reach the server",
    });
    assert.equal(await client.disconnectGroqProvider(), false);

    scenario = "providers-empty";
    assert.deepEqual(await client.fetchProviders(), []);
  } finally {
    mock.restore();
  }
});

test("module endpoints unwrap lists and toggle with POST bodies", async () => {
  let scenario = "list";
  const mock = installFetch((url, init) => {
    if (scenario === "throw") throw new TypeError("fetch failed");
    if (url === "/api/modules" && !init) {
      return scenario === "list"
        ? jsonResponse({ modules: [{ name: "assistant" }] })
        : jsonResponse({});
    }
    if (url === "/api/modules/toggle") {
      assert.deepEqual(JSON.parse(String(init?.body)), {
        module: "vision",
        enabled: true,
      });
      return scenario === "toggle-ok"
        ? jsonResponse({
            module: "vision",
            enabled: true,
            models_downloaded: false,
          })
        : jsonResponse({ detail: "not allowed" }, 400);
    }
    throw new Error(`Unexpected fetch in test: ${url}`);
  });
  try {
    assert.deepEqual(await client.fetchModules(), [{ name: "assistant" }]);

    scenario = "toggle-ok";
    assert.deepEqual(await client.toggleModule("vision", true), {
      module: "vision",
      enabled: true,
      models_downloaded: false,
    });

    scenario = "toggle-fail";
    assert.equal(await client.toggleModule("vision", true), null);

    scenario = "throw";
    assert.deepEqual(await client.fetchModules(), []);
    assert.equal(await client.toggleModule("vision", true), null);

    scenario = "empty";
    assert.deepEqual(await client.fetchModules(), []);
  } finally {
    mock.restore();
  }
});

test("installModuleModels() streams SSE events and skips malformed lines", async () => {
  const seen: unknown[] = [];
  const mock = installFetch(() =>
    sseResponse([
      `data: ${JSON.stringify({ event: "pull_progress", percent: 10 })}\n\n`,
      "data: {broken json\n\n",
      `data: ${JSON.stringify({ event: "install_done" })}\n\n`,
    ]),
  );
  try {
    const ok = await client.installModuleModels("vision", (event) =>
      seen.push(event),
    );
    assert.equal(ok, true);
    assert.deepEqual(seen, [
      { event: "pull_progress", percent: 10 },
      { event: "install_done" },
    ]);
  } finally {
    mock.restore();
  }

  const failing = installFetch(() => jsonResponse({}, 500));
  try {
    assert.equal(await client.installModuleModels("vision", () => {}), false);
  } finally {
    failing.restore();
  }
});

test("fetchConversations() builds the limit/offset/archived query exactly", async () => {
  const urls: string[] = [];
  const mock = installFetch((url) => {
    urls.push(url);
    return jsonResponse({ conversations: [] });
  });
  try {
    await client.fetchConversations(50, 0, false);
    await client.fetchConversations(100, 0, true);
    await client.fetchConversations();
    await client.fetchConversations(7, 21);
    assert.deepEqual(urls, [
      "/api/conversations?limit=50&offset=0&archived=false",
      "/api/conversations?limit=100&offset=0&archived=true",
      "/api/conversations?limit=50&offset=0",
      "/api/conversations?limit=7&offset=21",
    ]);
  } finally {
    mock.restore();
  }
});

test("conversation CRUD helpers use the right verbs and unwrapping", async () => {
  let scenario = "create";
  const mock = installFetch((url, init) => {
    if (scenario === "throw") throw new TypeError("fetch failed");
    switch (scenario) {
      case "create":
        assert.equal(init?.method, "POST");
        return jsonResponse({
          id: "c9",
          title: "New Chat",
          model: null,
          createdAt: 1,
          updatedAt: 1,
        });
      case "messages":
        assert.equal(url, "/api/conversations/c9");
        return jsonResponse({ messages: [{ id: "m1" }] });
      case "messages-empty":
        return jsonResponse({});
      case "delete":
        assert.equal(init?.method, "DELETE");
        return jsonResponse({});
      case "rename":
        assert.equal(init?.method, "PATCH");
        assert.equal(url, "/api/conversations/c9?title=Plan%20B");
        return jsonResponse({});
      case "flags-both":
        assert.equal(init?.method, "PATCH");
        assert.equal(url, "/api/conversations/c9?pinned=true&archived=false");
        return jsonResponse({});
      case "detail":
        return jsonResponse({
          conversation: {
            id: "c9",
            title: "T",
            model: null,
            createdAt: 1,
            updatedAt: 1,
          },
          messages: [{ id: "m2" }],
        });
      case "not-found":
        return jsonResponse({ detail: "nope" }, 404);
      default:
        throw new Error(`Unexpected scenario: ${scenario}`);
    }
  });
  try {
    scenario = "create";
    assert.equal((await client.createConversation()).id, "c9");
    assert.equal(
      mock.calls[mock.calls.length - 1].url,
      "/api/conversations?title=New+Chat",
    );

    scenario = "messages";
    assert.deepEqual(await client.fetchConversationMessages("c9"), [
      { id: "m1" },
    ]);

    scenario = "messages-empty";
    assert.deepEqual(await client.fetchConversationMessages("c9"), []);

    scenario = "delete";
    assert.equal(await client.deleteConversation("c9"), true);

    scenario = "rename";
    assert.equal(await client.updateConversationTitle("c9", "Plan B"), true);

    scenario = "flags-both";
    assert.equal(
      await client.updateConversationFlags("c9", {
        pinned: true,
        archived: false,
      }),
      true,
    );

    scenario = "detail";
    const detail = await client.fetchConversationDetail("c9");
    assert.equal(detail?.conversation.id, "c9");
    assert.deepEqual(detail?.messages, [{ id: "m2" }]);

    scenario = "not-found";
    assert.equal(await client.fetchConversationDetail("c9"), null);

    scenario = "throw";
    assert.equal(await client.createConversation(), null);
    assert.deepEqual(await client.fetchConversationMessages("c9"), []);
    assert.equal(await client.deleteConversation("c9"), false);
    assert.equal(await client.updateConversationTitle("c9", "x"), false);
    assert.equal(
      await client.updateConversationFlags("c9", { pinned: true }),
      false,
    );
    assert.equal(await client.fetchConversationDetail("c9"), null);
  } finally {
    mock.restore();
  }
});

test("searchPastConversations() posts with query params and unwraps results", async () => {
  let ok = true;
  const mock = installFetch((url, init) => {
    assert.equal(init?.method, "POST");
    assert.equal(
      url,
      "/api/conversations/search?query=cats&limit=10&exclude_conversation_id=c1",
    );
    return ok
      ? jsonResponse({ results: [{ message_id: "m1" }] })
      : jsonResponse({}, 500);
  });
  try {
    assert.deepEqual(await client.searchPastConversations("cats", "c1"), [
      { message_id: "m1" },
    ]);
    ok = false;
    assert.deepEqual(await client.searchPastConversations("cats", "c1"), []);
  } finally {
    mock.restore();
  }
});
