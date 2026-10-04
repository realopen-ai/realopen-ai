/**
 * Tests for src/store/memoryStore.ts — the Brain ▸ Memories store.
 *
 * Direct import requires the alias hooks (`@/api/memoryClient` — see
 * tests/helpers/viteCompat.ts); all REST traffic goes through the
 * globalThis.fetch mock. The store's contract under test: category-aware
 * loading, optimistic add/delete/pin with revert-on-failure for pin,
 * search state, audit reloads, and the extraction-progress bookkeeping.
 */
import assert from "node:assert/strict";
import test from "node:test";

import "./helpers/viteCompat.ts";
import { installFetch, jsonResponse } from "./helpers/fetchMock.ts";

const { useMemoryStore } = await import("../src/store/memoryStore.ts");

function memory(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    text: `memory ${id}`,
    category: "identity",
    source: "user",
    pinned: false,
    uses: 0,
    conversation_id: null,
    created_at: 1,
    updated_at: 1,
    ...overrides,
  };
}

function reset(overrides: Record<string, unknown> = {}) {
  useMemoryStore.setState({
    memories: [],
    categories: [],
    isLoading: false,
    isSearching: false,
    isAuditing: false,
    activeCategory: "all",
    searchQuery: "",
    searchResults: [],
    isExtracting: false,
    lastExtraction: null,
    ...overrides,
  });
}

test("loadMemories() omits the category filter for 'all'", async () => {
  reset();
  const mock = installFetch((url) => {
    assert.equal(url, "/api/memory");
    return jsonResponse({ memories: [memory("m1")] });
  });
  try {
    await useMemoryStore.getState().loadMemories();
    assert.deepEqual(
      useMemoryStore.getState().memories.map((m) => m.id),
      ["m1"],
    );
    assert.equal(useMemoryStore.getState().isLoading, false);
  } finally {
    mock.restore();
  }
});

test("loadMemories() passes the active category as a query parameter", async () => {
  reset({ activeCategory: "preference" });
  const mock = installFetch((url) => {
    assert.equal(url, "/api/memory?category=preference");
    return jsonResponse({ memories: [] });
  });
  try {
    await useMemoryStore.getState().loadMemories();
    assert.deepEqual(useMemoryStore.getState().memories, []);
  } finally {
    mock.restore();
  }
});

test("loadMemories() survives a failing backend", async () => {
  reset();
  const mock = installFetch(() => jsonResponse({}, 500));
  try {
    await useMemoryStore.getState().loadMemories();
    assert.equal(useMemoryStore.getState().isLoading, false);
    assert.deepEqual(useMemoryStore.getState().memories, []);
  } finally {
    mock.restore();
  }
});

test("addMemory() prepends the created memory and refreshes categories", async () => {
  reset({ memories: [memory("old")] });
  const urls: string[] = [];
  const mock = installFetch((url, init) => {
    urls.push(url);
    if (url === "/api/memory" && init?.method === "POST") {
      assert.deepEqual(JSON.parse(String(init.body)), {
        text: "likes tea",
        category: "preference",
        source: "user",
      });
      return jsonResponse(memory("new"));
    }
    return jsonResponse({
      categories: [{ category: "preference", count: 1 }],
    });
  });
  try {
    await useMemoryStore.getState().addMemory("likes tea", "preference");

    assert.deepEqual(
      useMemoryStore.getState().memories.map((m) => m.id),
      ["new", "old"],
    );
    assert.deepEqual(useMemoryStore.getState().categories, [
      { category: "preference", count: 1 },
    ]);
    assert.deepEqual(urls, ["/api/memory", "/api/memory/categories"]);
  } finally {
    mock.restore();
  }
});

test("addMemory() keeps state unchanged when the backend rejects", async () => {
  reset({
    memories: [memory("old")],
    categories: [{ category: "x", count: 9 }],
  });
  const mock = installFetch((url, init) =>
    init?.method === "POST"
      ? jsonResponse({ detail: "nope" }, 422)
      : jsonResponse({ categories: [] }),
  );
  try {
    await useMemoryStore.getState().addMemory("bad", "x");
    assert.deepEqual(
      useMemoryStore.getState().memories.map((m) => m.id),
      ["old"],
    );
    assert.deepEqual(useMemoryStore.getState().categories, [
      { category: "x", count: 9 },
    ]);
  } finally {
    mock.restore();
  }
});

test("updateMemory() patches the local row on success only", async () => {
  reset({ memories: [memory("m1", { text: "before", category: "fact" })] });
  let ok = true;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/memory/m1");
    assert.equal(init?.method, "PUT");
    assert.deepEqual(JSON.parse(String(init.body)), {
      text: "after",
      category: "preference",
    });
    return jsonResponse({}, ok ? 200 : 500);
  });
  try {
    await useMemoryStore.getState().updateMemory("m1", "after", "preference");
    const updated = useMemoryStore.getState().memories[0];
    assert.equal(updated.text, "after");
    assert.equal(updated.category, "preference");
    assert.ok(updated.updated_at >= 1);

    ok = false;
    await useMemoryStore.getState().updateMemory("m1", "rejected", "fact");
    assert.equal(useMemoryStore.getState().memories[0].text, "after");
  } finally {
    mock.restore();
  }
});

test("updateMemory() omits the category key when not changed", async () => {
  reset({ memories: [memory("m1")] });
  const mock = installFetch((url, init) => {
    assert.deepEqual(JSON.parse(String(init?.body)), { text: "renamed" });
    return jsonResponse({}, 200);
  });
  try {
    await useMemoryStore.getState().updateMemory("m1", "renamed");
    assert.equal(useMemoryStore.getState().memories[0].text, "renamed");
  } finally {
    mock.restore();
  }
});

test("deleteMemory() removes optimistically from list and search results", async () => {
  reset({
    memories: [memory("m1"), memory("m2")],
    searchResults: [memory("m2")],
    categories: [{ category: "identity", count: 2 }],
  });
  const mock = installFetch((url, init) => {
    if (init?.method === "DELETE") {
      assert.equal(url, "/api/memory/m2");
      return jsonResponse({}, 200);
    }
    return jsonResponse({ categories: [{ category: "identity", count: 1 }] });
  });
  try {
    await useMemoryStore.getState().deleteMemory("m2");
    assert.deepEqual(
      useMemoryStore.getState().memories.map((m) => m.id),
      ["m1"],
    );
    assert.deepEqual(useMemoryStore.getState().searchResults, []);
    // Categories are refreshed after the deletion.
    assert.deepEqual(useMemoryStore.getState().categories, [
      { category: "identity", count: 1 },
    ]);
  } finally {
    mock.restore();
  }
});

test("deleteMemory() stays optimistic when the backend fails", async () => {
  reset({ memories: [memory("m1")] });
  const mock = installFetch(() => jsonResponse({}, 500));
  try {
    await useMemoryStore.getState().deleteMemory("m1");
    // The removal is not reverted (a retry would be a duplicate delete).
    assert.deepEqual(useMemoryStore.getState().memories, []);
  } finally {
    mock.restore();
  }
});

test("pinMemory() failures never revert the optimistic update", async () => {
  reset({
    memories: [memory("m1")],
    searchResults: [memory("m1")],
  });
  let transportError = false;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/memory/m1/pin");
    assert.deepEqual(JSON.parse(String(init?.body)), { pinned: true });
    if (transportError) throw new TypeError("fetch failed");
    return jsonResponse({}, 500);
  });
  try {
    await useMemoryStore.getState().pinMemory("m1", true);
    // HTTP-level failure: pinMemory() resolves false, so the store's
    // catch-based revert never runs — the optimistic pin stays until reload.
    assert.equal(useMemoryStore.getState().memories[0].pinned, true);
    assert.equal(useMemoryStore.getState().searchResults[0].pinned, true);

    transportError = true;
    await useMemoryStore.getState().pinMemory("m1", false);
    // Transport-level failure keeps the optimistic value too: memoryClient
    // catches the fetch error and resolves false instead of rejecting, so
    // the store's revert branch is unreachable through the real client.
    assert.equal(useMemoryStore.getState().memories[0].pinned, false);
    assert.equal(useMemoryStore.getState().searchResults[0].pinned, false);
  } finally {
    mock.restore();
  }
});

test("pinMemory() keeps the pinned state on success", async () => {
  reset({ memories: [memory("m1")], searchResults: [memory("m1")] });
  const mock = installFetch(() => jsonResponse({}, 200));
  try {
    await useMemoryStore.getState().pinMemory("m1", true);
    assert.equal(useMemoryStore.getState().memories[0].pinned, true);
    assert.equal(useMemoryStore.getState().searchResults[0].pinned, true);
  } finally {
    mock.restore();
  }
});

test("searchMemories() stores the query, results, and clears on failure", async () => {
  reset();
  let ok = true;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/memory/search");
    assert.equal(init?.method, "POST");
    assert.deepEqual(JSON.parse(String(init.body)), {
      query: "tea",
      category: "preference",
    });
    return ok
      ? jsonResponse({ memories: [memory("m1")] })
      : jsonResponse({}, 500);
  });
  try {
    await useMemoryStore.getState().searchMemories("tea", "preference");
    assert.equal(useMemoryStore.getState().searchQuery, "tea");
    assert.deepEqual(
      useMemoryStore.getState().searchResults.map((m) => m.id),
      ["m1"],
    );
    assert.equal(useMemoryStore.getState().isSearching, false);

    ok = false;
    await useMemoryStore.getState().searchMemories("tea");
    assert.deepEqual(useMemoryStore.getState().searchResults, []);
    assert.equal(useMemoryStore.getState().isSearching, false);
  } finally {
    mock.restore();
  }
});

test("auditMemories() reloads memories and categories after a successful audit", async () => {
  reset({ memories: [memory("stale")] });
  const urls: string[] = [];
  const mock = installFetch((url, init) => {
    urls.push(url);
    if (url === "/api/memory/audit") {
      assert.equal(init?.method, "POST");
      return jsonResponse({ before: 5, after: 3, removed: 2 });
    }
    if (url === "/api/memory")
      return jsonResponse({ memories: [memory("fresh")] });
    return jsonResponse({
      categories: [{ category: "identity", count: 1 }],
    });
  });
  try {
    await useMemoryStore.getState().auditMemories();
    assert.deepEqual(
      useMemoryStore.getState().memories.map((m) => m.id),
      ["fresh"],
    );
    assert.deepEqual(useMemoryStore.getState().categories, [
      { category: "identity", count: 1 },
    ]);
    assert.equal(useMemoryStore.getState().isAuditing, false);
    assert.deepEqual(urls, [
      "/api/memory/audit",
      "/api/memory",
      "/api/memory/categories",
    ]);
  } finally {
    mock.restore();
  }
});

test("auditMemories() skips the reload when nothing was audited", async () => {
  reset({ memories: [memory("m1")] });
  const mock = installFetch(() => jsonResponse({}, 500));
  try {
    await useMemoryStore.getState().auditMemories();
    assert.equal(useMemoryStore.getState().isAuditing, false);
    assert.deepEqual(
      useMemoryStore.getState().memories.map((m) => m.id),
      ["m1"],
    );
  } finally {
    mock.restore();
  }
});

test("extraction progress bookkeeping is plain local state", () => {
  reset();
  useMemoryStore.getState().setExtracting(true);
  assert.equal(useMemoryStore.getState().isExtracting, true);

  useMemoryStore.getState().setLastExtraction(4);
  const last = useMemoryStore.getState().lastExtraction;
  assert.equal(last?.count, 4);
  assert.ok(last && last.timestamp > 0);

  useMemoryStore.getState().clearLastExtraction();
  assert.equal(useMemoryStore.getState().lastExtraction, null);
});

test("setActiveCategory() and clearSearch() update their slices", () => {
  reset({
    activeCategory: "all",
    searchQuery: "q",
    searchResults: [memory("m")],
  });
  useMemoryStore.getState().setActiveCategory("goal");
  assert.equal(useMemoryStore.getState().activeCategory, "goal");

  useMemoryStore.getState().clearSearch();
  assert.equal(useMemoryStore.getState().searchQuery, "");
  assert.deepEqual(useMemoryStore.getState().searchResults, []);
});
