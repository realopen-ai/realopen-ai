/**
 * Tests for src/store/aiStore.ts — Settings ▸ AI model/provider state.
 *
 * Direct import requires the alias hooks (`@/api/client`, `@/store/chatStore`
 * — see tests/helpers/viteCompat.ts); REST traffic is served by the
 * globalThis.fetch mock. The cross-store contract under test: the Chat/Agent
 * task slot drives useChatStore's selected model, and provider connect/
 * disconnect refresh the model list.
 */
import assert from "node:assert/strict";
import test from "node:test";

import "./helpers/viteCompat.ts";
import { installFetch, jsonResponse } from "./helpers/fetchMock.ts";

const { useAiStore } = await import("../src/store/aiStore.ts");
const { useChatStore } = await import("../src/store/chatStore.ts");

const chatTask = {
  task: "chat",
  label: "Chat / Agent",
  model: "qwen3:4b",
  provider: "ollama" as const,
  default_model: "qwen3:4b",
  is_default: true,
  local_only: false,
};

const modelsPayload = (model: string) => ({
  models: [
    {
      id: model,
      provider: "ollama",
      installed: true,
      description: "",
      size: "2.5 GB",
      type: "chat",
    },
  ],
  tasks: [{ ...chatTask, model }],
  groq_connected: true,
  ollama_connected: true,
});

function reset() {
  useAiStore.setState({
    models: [],
    tasks: [chatTask],
    groqConnected: false,
    ollamaConnected: false,
    isLoadingModels: false,
    providers: [],
    isLoadingProviders: false,
    isConnectingGroq: false,
    groqError: null,
    taskErrors: {},
  });
  useChatStore.setState({ selectedModel: "default" });
}

test("load() fills model slots and refreshes providers in one pass", async () => {
  reset();
  const urls: string[] = [];
  const mock = installFetch((url) => {
    urls.push(url);
    if (url === "/api/models/available")
      return jsonResponse(modelsPayload("qwen3:4b"));
    if (url === "/api/providers")
      return jsonResponse({
        providers: [
          {
            id: "groq",
            name: "Groq",
            kind: "cloud",
            connected: true,
            model_count: 3,
          },
        ],
      });
    throw new Error(`Unexpected fetch in test: ${url}`);
  });
  try {
    await useAiStore.getState().load();

    const s = useAiStore.getState();
    assert.equal(s.models.length, 1);
    assert.equal(s.tasks[0].model, "qwen3:4b");
    assert.equal(s.groqConnected, true);
    assert.equal(s.ollamaConnected, true);
    assert.equal(s.isLoadingModels, false);
    assert.deepEqual(s.providers, [
      {
        id: "groq",
        name: "Groq",
        kind: "cloud",
        connected: true,
        model_count: 3,
      },
    ]);
    assert.equal(s.isLoadingProviders, false);
    assert.deepEqual(urls, ["/api/models/available", "/api/providers"]);
  } finally {
    mock.restore();
  }
});

test("load() keeps defaults when the models endpoint fails but still loads providers", async () => {
  reset();
  const mock = installFetch((url) =>
    url === "/api/models/available"
      ? jsonResponse({}, 500)
      : jsonResponse({ providers: [] }),
  );
  try {
    await useAiStore.getState().load();
    const s = useAiStore.getState();
    assert.deepEqual(s.models, []);
    assert.equal(s.groqConnected, false);
    assert.equal(s.isLoadingModels, false);
    assert.deepEqual(s.providers, []);
  } finally {
    mock.restore();
  }
});

test("selectModel() optimistically switches the slot and syncs the chat store for the chat task", async () => {
  reset();
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/models/preferences");
    assert.equal(init?.method, "PUT");
    assert.deepEqual(JSON.parse(String(init.body)), {
      task: "chat",
      model: "llama3:8b",
    });
    return jsonResponse({ ...chatTask, model: "llama3:8b", is_default: false });
  });
  try {
    await useAiStore.getState().selectModel("chat", "llama3:8b");

    const slot = useAiStore.getState().tasks[0];
    assert.equal(slot.model, "llama3:8b");
    assert.equal(slot.is_default, false);
    // The chat slot drives the model the next message is sent with.
    assert.equal(useChatStore.getState().selectedModel, "llama3:8b");
    assert.equal(useAiStore.getState().taskErrors.chat, null);
  } finally {
    mock.restore();
  }
});

test("selectModel() shows the optimistic switch while the save is in flight", async () => {
  reset();
  let finish!: () => void;
  const mock = installFetch(
    () =>
      new Promise<Response>((resolve) => {
        finish = () =>
          resolve(jsonResponse({ ...chatTask, model: "llama3:8b" }));
      }),
  );
  try {
    const pending = useAiStore.getState().selectModel("chat", "llama3:8b");
    assert.equal(useAiStore.getState().tasks[0].model, "llama3:8b");
    assert.equal(useAiStore.getState().taskErrors.chat, null);

    finish();
    await pending;
    assert.equal(useAiStore.getState().tasks[0].model, "llama3:8b");
  } finally {
    mock.restore();
  }
});

test("selectModel(null) resets the slot to its default model", async () => {
  reset();
  useAiStore.setState({
    tasks: [{ ...chatTask, model: "llama3:8b", is_default: false }],
  });
  const mock = installFetch((url, init) => {
    assert.deepEqual(JSON.parse(String(init?.body)), {
      task: "chat",
      model: null,
    });
    return jsonResponse({ ...chatTask, is_default: true });
  });
  try {
    await useAiStore.getState().selectModel("chat", null);
    const slot = useAiStore.getState().tasks[0];
    assert.equal(slot.model, chatTask.default_model);
    assert.equal(slot.is_default, true);
  } finally {
    mock.restore();
  }
});

test("selectModel() failure reloads server state and surfaces saveFailed", async () => {
  reset();
  useAiStore.setState({
    tasks: [{ ...chatTask, model: "llama3:8b", is_default: false }],
  });
  let preferencesFail = true;
  const mock = installFetch((url) => {
    if (url === "/api/models/preferences" && preferencesFail) {
      return jsonResponse({ detail: "boom" }, 500);
    }
    if (url === "/api/models/available")
      return jsonResponse(modelsPayload("qwen3:4b"));
    if (url === "/api/providers") return jsonResponse({ providers: [] });
    throw new Error(`Unexpected fetch in test: ${url}`);
  });
  try {
    await useAiStore.getState().selectModel("chat", "mistral:7b");

    assert.equal(useAiStore.getState().taskErrors.chat, "saveFailed");
    // The failed optimistic switch is rolled back by reloading the server.
    assert.equal(useAiStore.getState().tasks[0].model, "qwen3:4b");
  } finally {
    mock.restore();
  }
});

test("connectGroq() success clears the error and refreshes the model list", async () => {
  reset();
  const urls: string[] = [];
  const mock = installFetch((url, init) => {
    urls.push(url);
    if (url === "/api/providers/groq") {
      assert.equal(init?.method, "POST");
      assert.deepEqual(JSON.parse(String(init.body)), { api_key: "gsk_x" });
      return jsonResponse({ key_masked: "gsk_…ab" });
    }
    if (url === "/api/models/available")
      return jsonResponse(modelsPayload("groq-model"));
    if (url === "/api/providers") return jsonResponse({ providers: [] });
    throw new Error(`Unexpected fetch in test: ${url}`);
  });
  try {
    const ok = await useAiStore.getState().connectGroq("gsk_x");

    assert.equal(ok, true);
    assert.equal(useAiStore.getState().groqError, null);
    assert.equal(useAiStore.getState().isConnectingGroq, false);
    // Models were reloaded after connecting — Groq models become selectable.
    assert.equal(useAiStore.getState().models[0].id, "groq-model");
    assert.deepEqual(urls, [
      "/api/providers/groq",
      "/api/models/available",
      "/api/providers",
    ]);
  } finally {
    mock.restore();
  }
});

test("connectGroq() failure surfaces the server's message and skips the reload", async () => {
  reset();
  const mock = installFetch((url) => {
    assert.equal(url, "/api/providers/groq");
    return jsonResponse({ detail: "Invalid Groq key" }, 401);
  });
  try {
    const ok = await useAiStore.getState().connectGroq("bad");

    assert.equal(ok, false);
    assert.equal(useAiStore.getState().groqError, "Invalid Groq key");
    assert.equal(useAiStore.getState().isConnectingGroq, false);
    assert.deepEqual(useAiStore.getState().models, []);
  } finally {
    mock.restore();
  }
});

test("connectGroq() maps transport errors to a friendly message", async () => {
  reset();
  const mock = installFetch(() => {
    throw new TypeError("fetch failed");
  });
  try {
    const ok = await useAiStore.getState().connectGroq("gsk_x");
    assert.equal(ok, false);
    assert.equal(useAiStore.getState().groqError, "Cannot reach the server");
  } finally {
    mock.restore();
  }
});

test("disconnectGroq() reloads only on success", async () => {
  reset();
  let ok = true;
  let modelFetches = 0;
  const mock = installFetch((url, init) => {
    if (url === "/api/providers/groq") {
      assert.equal(init?.method, "DELETE");
      return jsonResponse({}, ok ? 200 : 500);
    }
    if (url === "/api/models/available") {
      modelFetches += 1;
      return jsonResponse(modelsPayload("qwen3:4b"));
    }
    if (url === "/api/providers") return jsonResponse({ providers: [] });
    throw new Error(`Unexpected fetch in test: ${url}`);
  });
  try {
    await useAiStore.getState().disconnectGroq();
    assert.equal(modelFetches, 1);

    ok = false;
    await useAiStore.getState().disconnectGroq();
    assert.equal(modelFetches, 1);
  } finally {
    mock.restore();
  }
});
