/**
 * Tests for src/store/toolsStore.ts — the Brain ▸ Tools configuration store.
 *
 * Direct import is impossible without the alias hooks (the module imports
 * `@/api/toolsClient` and `@/lib/debug`, which Node cannot resolve — see
 * tests/helpers/viteCompat.ts), so this file imports the hooks first and
 * then dynamically imports the store. The REST calls the store makes
 * through toolsClient are served by a globalThis.fetch mock; no request
 * ever leaves the process.
 */
import assert from "node:assert/strict";
import test from "node:test";

import "./helpers/viteCompat.ts";
import {
  installFetch,
  jsonResponse,
  type RecordedFetch,
} from "./helpers/fetchMock.ts";

const { useToolsStore } = await import("../src/store/toolsStore.ts");

function reset() {
  useToolsStore.setState({
    tools: [],
    isLoading: false,
    saveErrors: {},
    saving: {},
  });
}

const websearchTool = {
  tool: "websearch",
  display_name: "Web Search",
  description: "",
  tool_type: "custom",
  config: {
    enabled: true,
    always_load: false,
    tags: [],
    model: null,
    custom: {},
  },
  custom_schema: null,
  has_custom: false,
  effective_model: null,
  secrets: {},
};

test("load() fetches the tool list and clears the loading flag", async () => {
  reset();
  let finish!: () => void;
  const mock = installFetch(
    () =>
      new Promise<Response>((resolve) => {
        finish = () => resolve(jsonResponse({ tools: [websearchTool] }));
      }),
  );
  try {
    const pending = useToolsStore.getState().load();
    assert.equal(useToolsStore.getState().isLoading, true);
    finish();
    await pending;

    assert.equal(useToolsStore.getState().isLoading, false);
    assert.deepEqual(useToolsStore.getState().tools, [websearchTool]);
    assert.deepEqual(
      mock.calls.map((c) => c.url),
      ["/api/tools"],
    );
  } finally {
    mock.restore();
  }
});

test("updateConfig() PUTs the config and swaps in the server's tool row", async () => {
  reset();
  const updatedTool = {
    ...websearchTool,
    config: {
      ...websearchTool.config,
      enabled: false,
      tags: ["search"],
    },
  };
  let putBody: Record<string, unknown> | undefined;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/tools/websearch");
    assert.equal(init?.method, "PUT");
    putBody = JSON.parse(String(init?.body));
    return jsonResponse(updatedTool);
  });
  try {
    useToolsStore.setState({
      tools: [websearchTool],
      saveErrors: { websearch: "old error" },
    });
    const ok = await useToolsStore
      .getState()
      .updateConfig("websearch", { enabled: false, tags: ["search"] });

    assert.equal(ok, true);
    // One atomic PUT body: {config, ...} — no secrets key when none given.
    assert.deepEqual(putBody, { config: { enabled: false, tags: ["search"] } });
    // The row is replaced wholesale by the server's response...
    assert.deepEqual(useToolsStore.getState().tools, [updatedTool]);
    // ...the stale error is cleared and no save stays in flight.
    assert.equal(useToolsStore.getState().saveErrors.websearch, null);
    assert.equal(useToolsStore.getState().saving.websearch, false);
  } finally {
    mock.restore();
  }
});

test("updateConfig() tracks the in-flight save and blocks on it", async () => {
  reset();
  let finish!: () => void;
  const mock = installFetch(
    () =>
      new Promise<Response>((resolve) => {
        finish = () => resolve(jsonResponse(websearchTool));
      }),
  );
  try {
    const pending = useToolsStore
      .getState()
      .updateConfig("websearch", { enabled: true });
    // saving[tool] is true for the whole flight, and the previous error
    // is cleared optimistically.
    assert.equal(useToolsStore.getState().saving.websearch, true);
    assert.equal(useToolsStore.getState().saveErrors.websearch, null);

    finish();
    assert.equal(await pending, true);
    assert.equal(useToolsStore.getState().saving.websearch, false);
  } finally {
    mock.restore();
  }
});

test("updateConfig() surfaces the server's detail message on rejection", async () => {
  reset();
  const mock = installFetch(() =>
    jsonResponse({ detail: "Invalid API key" }, 422),
  );
  try {
    const ok = await useToolsStore
      .getState()
      .updateConfig("websearch", { enabled: true });

    assert.equal(ok, false);
    assert.equal(
      useToolsStore.getState().saveErrors.websearch,
      "Invalid API key",
    );
    assert.equal(useToolsStore.getState().saving.websearch, false);
    // Nothing was applied — the row list stays untouched.
    assert.deepEqual(useToolsStore.getState().tools, []);
  } finally {
    mock.restore();
  }
});

test("updateConfig() reports an unreachable backend without throwing", async () => {
  reset();
  const mock = installFetch(() => {
    throw new TypeError("fetch failed");
  });
  try {
    const ok = await useToolsStore
      .getState()
      .updateConfig("websearch", { enabled: true });
    assert.equal(ok, false);
    // toolsClient maps transport errors to this user-facing message.
    assert.equal(
      useToolsStore.getState().saveErrors.websearch,
      "Cannot reach the server",
    );
  } finally {
    mock.restore();
  }
});

test("updateSecret() sends secrets under the secrets key, null clears", async () => {
  reset();
  const puts: RecordedFetch[] = [];
  const mock = installFetch((url, init) => {
    puts.push({ url, init });
    return jsonResponse(websearchTool);
  });
  try {
    assert.equal(
      await useToolsStore
        .getState()
        .updateSecret("websearch", "api_key", "sk-123"),
      true,
    );
    assert.equal(
      await useToolsStore.getState().updateSecret("websearch", "api_key", null),
      true,
    );

    assert.deepEqual(
      puts.map((c) => JSON.parse(String(c.init?.body))),
      [
        // Secrets ride alongside an empty config patch, never inside it.
        { config: {}, secrets: { api_key: "sk-123" } },
        { config: {}, secrets: { api_key: null } },
      ],
    );
    assert.deepEqual(
      puts.map((c) => c.url),
      ["/api/tools/websearch", "/api/tools/websearch"],
    );
  } finally {
    mock.restore();
  }
});

test("updateSecret() records the failure error per tool", async () => {
  reset();
  const mock = installFetch(() =>
    jsonResponse({ detail: "Secret validation failed" }, 400),
  );
  try {
    assert.equal(
      await useToolsStore
        .getState()
        .updateSecret("websearch", "api_key", "bad"),
      false,
    );
    assert.equal(
      useToolsStore.getState().saveErrors.websearch,
      "Secret validation failed",
    );
  } finally {
    mock.restore();
  }
});

test("applyTool() replaces only the matching row", () => {
  reset();
  const other = { ...websearchTool, tool: "vision" };
  const replacement = { ...websearchTool, display_name: "Web Search v2" };
  useToolsStore.setState({ tools: [other, websearchTool] });

  useToolsStore.getState().applyTool(replacement);

  const tools = useToolsStore.getState().tools;
  assert.equal(tools.length, 2);
  assert.equal(tools[0].tool, "vision"); // untouched
  assert.equal(tools[1].display_name, "Web Search v2"); // replaced in place
});
