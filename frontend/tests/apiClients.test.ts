/**
 * Tests for the domain API clients: src/api/toolsClient.ts,
 * src/api/memoryClient.ts, src/api/skillsClient.ts, src/api/depsClient.ts.
 *
 * toolsClient/memoryClient import `@/lib/debug`, so the alias hooks are
 * needed (tests/helpers/viteCompat.ts); skillsClient/depsClient have no
 * imports at all. All traffic is served by the globalThis.fetch mock:
 * unwrapping, URL encoding, request bodies, error-detail surfacing, and the
 * SSE install stream of depsClient are exercised against real Response
 * objects.
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

const toolsClient = await import("../src/api/toolsClient.ts");
const memoryClient = await import("../src/api/memoryClient.ts");
// skillsClient/depsClient have no @/ imports — a plain static import works,
// but they are imported dynamically here for symmetry with the hooked ones.
const { skillsClient } = await import("../src/api/skillsClient.ts");
const depsClient = await import("../src/api/depsClient.ts");

// ── toolsClient ─────────────────────────────────────────────────────────

test("toolsClient fetches the tool list and unwraps data.tools", async () => {
  const mock = installFetch((url) => {
    assert.equal(url, "/api/tools");
    return jsonResponse({ tools: [{ tool: "websearch" }] });
  });
  try {
    assert.deepEqual(await toolsClient.fetchTools(), [{ tool: "websearch" }]);
  } finally {
    mock.restore();
  }

  const failing = installFetch(() => jsonResponse({}, 500));
  try {
    assert.deepEqual(await toolsClient.fetchTools(), []);
  } finally {
    failing.restore();
  }
});

test("toolsClient fetches one tool by encoded name", async () => {
  const mock = installFetch((url) => {
    assert.equal(url, "/api/tools/web%20search");
    return jsonResponse({ tool: "web search" });
  });
  try {
    assert.deepEqual(await toolsClient.fetchTool("web search"), {
      tool: "web search",
    });
  } finally {
    mock.restore();
  }
});

test("toolsClient.updateTool() sends config and optional secrets", async () => {
  const visionTool = { tool: "vision", display_name: "Vision" };
  let scenario: "ok" | "detail" | "no-detail" | "throw" = "ok";
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/tools/vision");
    assert.equal(init?.method, "PUT");
    if (scenario === "throw") throw new TypeError("fetch failed");
    if (scenario === "ok") {
      assert.deepEqual(JSON.parse(String(init?.body)), {
        config: { enabled: true },
        secrets: { api_key: "sk" },
      });
      return jsonResponse(visionTool);
    }
    return jsonResponse(
      scenario === "detail" ? { detail: "bad key" } : {},
      422,
    );
  });
  try {
    assert.deepEqual(
      await toolsClient.updateTool(
        "vision",
        { enabled: true },
        { api_key: "sk" },
      ),
      { tool: visionTool },
    );

    scenario = "detail";
    assert.deepEqual(await toolsClient.updateTool("vision", {}, {}), {
      error: "bad key",
    });

    scenario = "no-detail";
    assert.deepEqual(await toolsClient.updateTool("vision", {}, {}), {
      error: "Request failed (422)",
    });

    scenario = "throw";
    assert.deepEqual(await toolsClient.updateTool("vision", {}, {}), {
      error: "Cannot reach the server",
    });
  } finally {
    mock.restore();
  }
});

// ── memoryClient ────────────────────────────────────────────────────────

test("memoryClient.fetchMemories() encodes the category and unwraps both shapes", async () => {
  let shape = "wrapped";
  const mock = installFetch((url) => {
    assert.equal(url, "/api/memory?category=personal%20facts");
    return shape === "wrapped"
      ? jsonResponse({ memories: [{ id: "m1" }] })
      : jsonResponse([{ id: "bare" }]);
  });
  try {
    assert.deepEqual(await memoryClient.fetchMemories("personal facts"), [
      { id: "m1" },
    ]);
    shape = "bare";
    assert.deepEqual(await memoryClient.fetchMemories("personal facts"), [
      { id: "bare" },
    ]);
  } finally {
    mock.restore();
  }
});

test("memoryClient CRUD helpers use the right verbs and bodies", async () => {
  let scenario = "add";
  const mock = installFetch((url, init) => {
    if (scenario === "throw") throw new TypeError("fetch failed");
    switch (scenario) {
      case "add":
        assert.equal(url, "/api/memory");
        assert.equal(init?.method, "POST");
        assert.deepEqual(JSON.parse(String(init?.body)), {
          text: "likes tea",
          category: "preference",
          source: "user",
        });
        return jsonResponse({ id: "m1" });
      case "update":
        assert.equal(url, "/api/memory/m1");
        assert.equal(init?.method, "PUT");
        assert.deepEqual(JSON.parse(String(init?.body)), {
          text: "likes coffee",
        });
        return jsonResponse({});
      case "delete":
        assert.equal(url, "/api/memory/m1");
        assert.equal(init?.method, "DELETE");
        return jsonResponse({});
      case "pin":
        assert.equal(url, "/api/memory/m1/pin");
        assert.equal(init?.method, "POST");
        assert.deepEqual(JSON.parse(String(init?.body)), { pinned: true });
        return jsonResponse({});
      case "search":
        assert.equal(url, "/api/memory/search");
        assert.deepEqual(JSON.parse(String(init?.body)), {
          query: "tea",
          category: "preference",
        });
        return jsonResponse({ memories: [{ id: "hit" }] });
      case "audit":
        assert.equal(url, "/api/memory/audit");
        assert.equal(init?.method, "POST");
        return jsonResponse({ before: 3, after: 2, removed: 1 });
      case "categories":
        assert.equal(url, "/api/memory/categories");
        return jsonResponse([{ category: "preference", count: 2 }]);
      default:
        throw new Error(`Unexpected scenario: ${scenario}`);
    }
  });
  try {
    assert.deepEqual(
      await memoryClient.addMemory("likes tea", "preference", "user"),
      {
        id: "m1",
      },
    );

    scenario = "update";
    assert.equal(await memoryClient.updateMemory("m1", "likes coffee"), true);

    scenario = "delete";
    assert.equal(await memoryClient.deleteMemory("m1"), true);

    scenario = "pin";
    assert.equal(await memoryClient.pinMemory("m1", true), true);

    scenario = "search";
    assert.deepEqual(await memoryClient.searchMemories("tea", "preference"), [
      { id: "hit" },
    ]);

    scenario = "audit";
    assert.deepEqual(await memoryClient.auditMemories(), {
      before: 3,
      after: 2,
      removed: 1,
    });

    scenario = "categories";
    assert.deepEqual(await memoryClient.fetchMemoryCategories(), [
      { category: "preference", count: 2 },
    ]);

    scenario = "throw";
    assert.deepEqual(await memoryClient.fetchMemories(), []);
    assert.deepEqual(await memoryClient.fetchMemoryCategories(), []);
    assert.equal(await memoryClient.addMemory("x", "y"), null);
    assert.equal(await memoryClient.updateMemory("m1", "x"), false);
    assert.equal(await memoryClient.deleteMemory("m1"), false);
    assert.equal(await memoryClient.pinMemory("m1", true), false);
    assert.deepEqual(await memoryClient.searchMemories("x"), []);
    assert.equal(await memoryClient.auditMemories(), null);
  } finally {
    mock.restore();
  }
});

// ── skillsClient ────────────────────────────────────────────────────────

test("skillsClient.list() and get() hit the REST routes", async () => {
  const mock = installFetch((url) => {
    if (url === "/api/skills") return jsonResponse([{ id: "s1" }]);
    assert.equal(url, "/api/skills/reports%20basic");
    return jsonResponse({ id: "reports basic" });
  });
  try {
    assert.deepEqual(await skillsClient.list(), [{ id: "s1" }]);
    assert.deepEqual(await skillsClient.get("reports basic"), {
      id: "reports basic",
    });
  } finally {
    mock.restore();
  }
});

test("skillsClient.resource() encodes each path segment separately", async () => {
  const mock = installFetch((url) => {
    // The id is encoded as one segment; the resource path keeps its slashes
    // but encodes each segment.
    assert.equal(url, "/api/skills/s%201/resources/docs/guide%202.md");
    return new Response("# guide", { status: 200 });
  });
  try {
    assert.equal(
      await skillsClient.resource("s 1", "docs/guide 2.md"),
      "# guide",
    );
  } finally {
    mock.restore();
  }
});

test("skillsClient create/update/remove/import send the right payloads", async () => {
  let scenario = "create";
  const mock = installFetch((url, init) => {
    if (scenario === "error") {
      return jsonResponse({ detail: "Name taken" }, 409);
    }
    switch (scenario) {
      case "create":
        assert.equal(url, "/api/skills");
        assert.equal(init?.method, "POST");
        assert.deepEqual(JSON.parse(String(init?.body)), {
          name: "Skill",
          description: "",
          roles: ["general"],
          enabled: true,
          content: "instructions",
        });
        return jsonResponse({ id: "new" });
      case "update":
        assert.equal(url, "/api/skills/new");
        assert.equal(init?.method, "PUT");
        return jsonResponse({ id: "new" });
      case "remove":
        assert.equal(url, "/api/skills/new");
        assert.equal(init?.method, "DELETE");
        return new Response(null, { status: 204 });
      case "import":
        assert.equal(url, "/api/skills/import");
        assert.equal(init?.method, "POST");
        assert.ok(init?.body instanceof FormData);
        return jsonResponse({ id: "imported" });
      default:
        throw new Error(`Unexpected scenario: ${scenario}`);
    }
  });
  try {
    assert.deepEqual(
      await skillsClient.create({
        name: "Skill",
        description: "",
        roles: ["general"],
        enabled: true,
        content: "instructions",
      }),
      { id: "new" },
    );

    scenario = "update";
    assert.deepEqual(
      await skillsClient.update("new", {
        name: "Skill",
        description: "",
        roles: [],
        enabled: false,
        content: "x",
      }),
      { id: "new" },
    );

    scenario = "remove";
    // 204 → no content, resolved as undefined.
    assert.equal(await skillsClient.remove("new"), undefined);

    scenario = "import";
    assert.deepEqual(await skillsClient.import(new FormData()), {
      id: "imported",
    });

    scenario = "error";
    await assert.rejects(() => skillsClient.list(), /Name taken/);
  } finally {
    mock.restore();
  }
});

// ── depsClient ──────────────────────────────────────────────────────────

test("depsClient.fetchDependencies() unwraps and swallows failures", async () => {
  const mock = installFetch((url) => {
    assert.equal(url, "/api/deps");
    return jsonResponse({ dependencies: [{ name: "libreoffice" }] });
  });
  try {
    assert.deepEqual(await depsClient.fetchDependencies(), [
      { name: "libreoffice" },
    ]);
  } finally {
    mock.restore();
  }

  const failing = installFetch(() => {
    throw new TypeError("fetch failed");
  });
  try {
    assert.deepEqual(await depsClient.fetchDependencies(), []);
  } finally {
    failing.restore();
  }
});

test("depsClient caches the LibreOffice probe until reset", async () => {
  const mock = installFetch((url) => {
    assert.equal(url, "/api/deps/libreoffice/status");
    return jsonResponse({ installed: true });
  });
  try {
    assert.equal(await depsClient.isLibreOfficeInstalled(), true);
    assert.equal(await depsClient.isLibreOfficeInstalled(), true);
    // One probe for both calls — the cache avoids hammering the endpoint.
    assert.equal(mock.calls.length, 1);

    depsClient.resetLibreOfficeCache();
    assert.equal(await depsClient.isLibreOfficeInstalled(), true);
    assert.equal(mock.calls.length, 2);
  } finally {
    mock.restore();
  }

  const negative = installFetch(() => jsonResponse({ installed: false }));
  try {
    depsClient.resetLibreOfficeCache();
    assert.equal(await depsClient.isLibreOfficeInstalled(), false);
    assert.equal(await depsClient.isLibreOfficeInstalled(), false);
    assert.equal(negative.calls.length, 1);
  } finally {
    negative.restore();
  }
});

test("depsClient.installDependency() streams events and stops at done", async () => {
  const seen: unknown[] = [];
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/deps/libreoffice/install");
    assert.equal(init?.method, "POST");
    return sseResponse([
      sseData({ stage: "checking_sudo", output: "ok" }),
      "data: {broken\n\n",
      sseData({ stage: "downloading", output: "50%" }),
      // Events after "done" are never read — the client stops there.
      sseData({ stage: "done", version: "7.6" }),
      sseData({ stage: "unreachable", output: "never seen" }),
    ]);
  });
  try {
    const final = await depsClient.installDependency("libreoffice", (e) =>
      seen.push(e),
    );
    // The terminal event is forwarded too; only lines after it are never read.
    assert.deepEqual(seen, [
      { stage: "checking_sudo", output: "ok" },
      { stage: "downloading", output: "50%" },
      { stage: "done", version: "7.6" },
    ]);
    assert.deepEqual(final, { stage: "done", version: "7.6" });
  } finally {
    mock.restore();
  }
});

test("depsClient.installDependency() surfaces error events and HTTP failures", async () => {
  const mock = installFetch(() =>
    sseResponse([
      sseData({ stage: "error", error: "disk full", exit_code: 1 }),
    ]),
  );
  try {
    const events: unknown[] = [];
    const final = await depsClient.installDependency("x", (e) =>
      events.push(e),
    );
    assert.deepEqual(events, [
      { stage: "error", error: "disk full", exit_code: 1 },
    ]);
    assert.equal(final.stage, "error");
  } finally {
    mock.restore();
  }

  const empty = installFetch(() => sseResponse([]));
  try {
    // A stream that ends without done/error resolves the sentinel event.
    const final = await depsClient.installDependency("x", () => {});
    assert.deepEqual(final, { stage: "error", error: "No response" });
  } finally {
    empty.restore();
  }

  const http = installFetch(() => jsonResponse({}, 500));
  try {
    await assert.rejects(
      () => depsClient.installDependency("x", () => {}),
      /HTTP 500/,
    );
  } finally {
    http.restore();
  }
});

test("depsClient.uninstallDependency() uses DELETE and the same stream contract", async () => {
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/deps/libreoffice/uninstall");
    assert.equal(init?.method, "DELETE");
    return sseResponse([
      sseData({ stage: "uninstalling", output: "…" }),
      sseData({ stage: "done" }),
    ]);
  });
  try {
    const events: unknown[] = [];
    const final = await depsClient.uninstallDependency("libreoffice", (e) =>
      events.push(e),
    );
    assert.deepEqual(events, [
      { stage: "uninstalling", output: "…" },
      { stage: "done" },
    ]);
    assert.equal(final.stage, "done");
  } finally {
    mock.restore();
  }
});
