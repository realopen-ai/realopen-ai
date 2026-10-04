/**
 * Tests for src/api/setupClient.ts and src/api/documentsClient.ts.
 *
 * Both import `@/lib/debug`, so the alias hooks are needed
 * (tests/helpers/viteCompat.ts); all traffic is served by the
 * globalThis.fetch mock. setupClient's pull-models SSE parser and
 * documentsClient's upload/re-index SSE parsers (progress/done/error
 * callbacks, malformed-line skipping, unexpected stream ends) run against
 * real streamed Response bodies.
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

const setupClient = await import("../src/api/setupClient.ts");
const documentsClient = await import("../src/api/documentsClient.ts");

// ── setupClient ─────────────────────────────────────────────────────────

test("setup status/hardware/profiles/modules endpoints unwrap or default", async () => {
  let scenario = "status";
  const mock = installFetch((url) => {
    switch (scenario) {
      case "status":
        assert.equal(url, "/api/setup/status");
        return jsonResponse({ setup_complete: true, profile: "cpu_small" });
      case "status-fail":
        return jsonResponse({}, 500);
      case "hardware":
        assert.equal(url, "/api/setup/hardware");
        return jsonResponse({ recommended_profile: "cpu_medium" });
      case "profiles":
        assert.equal(url, "/api/setup/profiles");
        return jsonResponse({ cpu_small: { label: "Small" } });
      case "modules":
        assert.equal(url, "/api/setup/modules");
        return jsonResponse({ modules: [{ name: "assistant" }] });
      default:
        throw new Error(`Unexpected fetch in test: ${url}`);
    }
  });
  try {
    assert.deepEqual(await setupClient.fetchSetupStatus(), {
      setup_complete: true,
      profile: "cpu_small",
    });

    scenario = "status-fail";
    // Failure degrades to "not set up" instead of throwing.
    assert.deepEqual(await setupClient.fetchSetupStatus(), {
      setup_complete: false,
      profile: null,
    });

    scenario = "hardware";
    assert.equal(
      (await setupClient.fetchHardwareInfo())?.recommended_profile,
      "cpu_medium",
    );

    scenario = "profiles";
    assert.deepEqual(await setupClient.fetchSetupProfiles(), {
      cpu_small: { label: "Small" },
    });

    scenario = "modules";
    assert.deepEqual(await setupClient.fetchSetupModules(), [
      { name: "assistant" },
    ]);
  } finally {
    mock.restore();
  }

  const failing = installFetch(() => {
    throw new TypeError("fetch failed");
  });
  try {
    assert.deepEqual(await setupClient.fetchSetupStatus(), {
      setup_complete: false,
      profile: null,
    });
    assert.equal(await setupClient.fetchHardwareInfo(), null);
    assert.equal(await setupClient.fetchSetupProfiles(), null);
    assert.equal(await setupClient.fetchSetupModules(), null);
  } finally {
    failing.restore();
  }
});

test("applySetup() posts the profile and module selection", async () => {
  let ok = true;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/setup/apply");
    assert.equal(init?.method, "POST");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      profile: "cpu_medium",
      enabled_modules: ["assistant", "vision"],
    });
    return ok
      ? jsonResponse({
          status: "ok",
          profile: "cpu_medium",
          enabled_modules: [],
          models_to_pull: [],
        })
      : jsonResponse({ detail: "nope" }, 500);
  });
  try {
    assert.equal(
      (await setupClient.applySetup("cpu_medium", ["assistant", "vision"]))
        ?.status,
      "ok",
    );
    ok = false;
    assert.equal(await setupClient.applySetup("cpu_medium", []), null);
  } finally {
    mock.restore();
  }
});

test("pullSetupModels() forwards SSE events in order and skips malformed lines", async () => {
  const seen: unknown[] = [];
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/setup/pull-models");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      profile: "cpu_small",
      enabled_modules: ["assistant"],
    });
    return sseResponse([
      sseData({ event: "pull_start", model: "qwen3:4b" }),
      "data: {broken\n\n",
      "event: ignored\n\n", // not a data: line
      sseData({ event: "pull_done", model: "qwen3:4b" }),
    ]);
  });
  try {
    const ok = await setupClient.pullSetupModels(
      "cpu_small",
      ["assistant"],
      (e) => seen.push(e),
    );
    assert.equal(ok, true);
    assert.deepEqual(seen, [
      { event: "pull_start", model: "qwen3:4b" },
      { event: "pull_done", model: "qwen3:4b" },
    ]);
  } finally {
    mock.restore();
  }

  const http = installFetch(() => jsonResponse({}, 503));
  try {
    assert.equal(
      await setupClient.pullSetupModels("cpu_small", [], () => {}),
      false,
    );
  } finally {
    http.restore();
  }
});

test("completeSetup() resolves the HTTP outcome", async () => {
  let ok = true;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/setup/complete");
    assert.equal(init?.method, "POST");
    return jsonResponse({}, ok ? 200 : 500);
  });
  try {
    assert.equal(await setupClient.completeSetup(), true);
    ok = false;
    assert.equal(await setupClient.completeSetup(), false);
  } finally {
    mock.restore();
  }
});

// ── documentsClient: list/get/update/delete ─────────────────────────────

test("document list endpoints build queries and unwrap data.documents", async () => {
  const urls: string[] = [];
  const mock = installFetch((url) => {
    urls.push(url);
    // Single-document GET returns the doc itself; lists wrap in documents.
    if (url === "/api/documents/d1") return jsonResponse({ id: "d1" });
    return jsonResponse({ documents: [{ id: "d1" }] });
  });
  try {
    assert.deepEqual(await documentsClient.listDocuments(), [{ id: "d1" }]);
    assert.deepEqual(
      await documentsClient.listDocuments({ scope: "private" }),
      [{ id: "d1" }],
    );
    assert.deepEqual(
      await documentsClient.listDocuments({
        scope: "private",
        conversationId: "c1",
      }),
      [{ id: "d1" }],
    );
    assert.deepEqual(await documentsClient.listDocumentsForConversation("c1"), [
      { id: "d1" },
    ]);
    assert.deepEqual(await documentsClient.getDocument("d1"), { id: "d1" });
    assert.deepEqual(urls, [
      "/api/documents",
      "/api/documents?scope=private",
      "/api/documents?scope=private&conversation_id=c1",
      "/api/conversations/c1/documents",
      "/api/documents/d1",
    ]);
  } finally {
    mock.restore();
  }
});

test("updateDocument() PATCHes and deleteDocument()/removeDocumentKnowledge() DELETE", async () => {
  let scenario = "update";
  const mock = installFetch((url, init) => {
    if (scenario === "throw") throw new TypeError("fetch failed");
    switch (scenario) {
      case "update":
        assert.equal(url, "/api/documents/d1");
        assert.equal(init?.method, "PATCH");
        assert.deepEqual(JSON.parse(String(init?.body)), {
          filename: "renamed.pdf",
          collections: ["Company"],
        });
        return jsonResponse({ id: "d1", filename: "renamed.pdf" });
      case "delete":
        assert.equal(init?.method, "DELETE");
        return jsonResponse({});
      case "knowledge":
        assert.equal(url, "/api/documents/d1/knowledge");
        assert.equal(init?.method, "DELETE");
        return jsonResponse({ id: "d1", digestion_status: "not_indexed" });
      default:
        throw new Error(`Unexpected scenario: ${scenario}`);
    }
  });
  try {
    assert.deepEqual(
      await documentsClient.updateDocument("d1", {
        filename: "renamed.pdf",
        collections: ["Company"],
      }),
      { id: "d1", filename: "renamed.pdf" },
    );

    scenario = "delete";
    assert.equal(await documentsClient.deleteDocument("d1"), true);

    scenario = "knowledge";
    assert.deepEqual(await documentsClient.removeDocumentKnowledge("d1"), {
      id: "d1",
      digestion_status: "not_indexed",
    });

    scenario = "throw";
    assert.equal(await documentsClient.updateDocument("d1", {}), null);
    assert.equal(await documentsClient.deleteDocument("d1"), false);
    assert.equal(await documentsClient.removeDocumentKnowledge("d1"), null);
  } finally {
    mock.restore();
  }
});

// ── documentsClient: SSE upload / re-index ──────────────────────────────

const doc = {
  id: "d1",
  filename: "a.pdf",
  original_filename: "a.pdf",
  mime_type: "application/pdf",
  file_size_bytes: 1,
  content_hash: null,
  scope: "public" as const,
  conversation_id: null,
  message_id: null,
  total_pages: 1,
  total_chunks: 2,
  total_images: 0,
  digestion_status: "ready" as const,
  digestion_error: null,
  collections: [],
  created_at: 1,
  updated_at: 1,
};

test("private uploads without a conversation id fail before any request", async () => {
  const mock = installFetch(() => {
    throw new Error("fetch must not be called");
  });
  try {
    await assert.rejects(
      () =>
        documentsClient.uploadDocumentStream(
          new File([new Uint8Array([1])], "a.pdf"),
          {
            scope: "private",
          },
        ),
      /conversationId is required for private uploads/,
    );
    assert.equal(mock.calls.length, 0);
  } finally {
    mock.restore();
  }
});

test("uploadDocumentStream() posts multipart and replays digestion progress", async () => {
  const progress: unknown[] = [];
  let done: unknown;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/documents/upload/stream");
    assert.equal(init?.method, "POST");
    const body = init?.body as FormData;
    assert.ok(body instanceof FormData);
    assert.equal((body.get("file") as File).name, "a.pdf");
    assert.equal(body.get("scope"), "private");
    assert.equal(body.get("conversation_id"), "c1");
    return sseResponse([
      sseData({
        event: "document_digest_progress",
        stage: "extracting_text",
        percent: 40,
        details: "reading",
        filename: "a.pdf",
        total_chunks: 2,
      }),
      "data: {broken\n\n",
      sseData({ event: "document_digest_done", document: doc }),
    ]);
  });
  try {
    await documentsClient.uploadDocumentStream(
      new File([new Uint8Array([1])], "a.pdf"),
      {
        scope: "private",
        conversationId: "c1",
        onProgress: (p) => progress.push(p),
        onDone: (d) => {
          done = d;
        },
      },
    );

    assert.deepEqual(progress, [
      {
        stage: "extracting_text",
        percent: 40,
        details: "reading",
        filename: "a.pdf",
        document_id: undefined,
        total_chunks: 2,
        total_images: undefined,
      },
      {
        stage: "done",
        percent: 100,
        details: "Digested a.pdf: 2 chunks",
        filename: "a.pdf",
        document_id: "d1",
        total_chunks: 2,
        total_images: 0,
      },
    ]);
    assert.deepEqual(done, doc);
  } finally {
    mock.restore();
  }
});

test("uploadDocumentStream() reports digest errors, HTTP failures, and dead streams", async () => {
  let scenario: "digest-error" | "http" | "dead-stream" | "transport" =
    "digest-error";
  const mock = installFetch(() => {
    switch (scenario) {
      case "digest-error":
        return sseResponse([
          sseData({
            event: "document_digest_error",
            filename: "a.pdf",
            error: "Corrupt PDF",
          }),
        ]);
      case "http":
        return new Response("too large", { status: 413 });
      case "dead-stream":
        return sseResponse([
          sseData({
            event: "document_digest_progress",
            stage: "embedding",
            percent: 80,
            details: "",
          }),
        ]);
      case "transport":
        throw new TypeError("fetch failed");
    }
  });
  const file = () => new File([new Uint8Array([1])], "a.pdf");
  try {
    const errors: string[] = [];
    await documentsClient.uploadDocumentStream(file(), {
      scope: "public",
      onError: (e) => errors.push(e),
    });
    assert.deepEqual(errors, ["Corrupt PDF"]);

    scenario = "http";
    errors.length = 0;
    await documentsClient.uploadDocumentStream(file(), {
      scope: "public",
      onError: (e) => errors.push(e),
    });
    assert.match(errors[0], /Upload failed \(HTTP 413\): too large/);

    scenario = "dead-stream";
    errors.length = 0;
    await documentsClient.uploadDocumentStream(file(), {
      scope: "public",
      onError: (e) => errors.push(e),
    });
    assert.deepEqual(errors, ["Upload stream ended unexpectedly"]);

    scenario = "transport";
    errors.length = 0;
    await documentsClient.uploadDocumentStream(file(), {
      scope: "public",
      onError: (e) => errors.push(e),
    });
    assert.deepEqual(errors, ["fetch failed"]);
  } finally {
    mock.restore();
  }
});

test("reindexDocumentStream() replays progress and terminal events", async () => {
  let scenario: "ok" | "error" | "dead" = "ok";
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/documents/d1/reindex/stream");
    assert.equal(init?.method, "POST");
    if (scenario === "ok") {
      return sseResponse([
        sseData({
          event: "document_digest_progress",
          stage: "embedding",
          percent: 10,
          details: "x",
        }),
        sseData({ event: "document_digest_done", document: doc }),
      ]);
    }
    if (scenario === "error") {
      return sseResponse([
        sseData({ event: "document_digest_error", error: "boom" }),
      ]);
    }
    return sseResponse([]);
  });
  try {
    const progress: unknown[] = [];
    let done: unknown;
    await documentsClient.reindexDocumentStream("d1", {
      onProgress: (p) => progress.push(p),
      onDone: (d) => {
        done = d;
      },
    });
    assert.deepEqual(progress, [
      {
        stage: "embedding",
        percent: 10,
        details: "x",
        document_id: undefined,
        total_chunks: undefined,
        total_images: undefined,
      },
    ]);
    assert.deepEqual(done, doc);

    scenario = "error";
    const errors: string[] = [];
    await documentsClient.reindexDocumentStream("d1", {
      onError: (e) => errors.push(e),
    });
    assert.deepEqual(errors, ["boom"]);

    scenario = "dead";
    errors.length = 0;
    await documentsClient.reindexDocumentStream("d1", {
      onError: (e) => errors.push(e),
    });
    assert.deepEqual(errors, ["Re-index stream ended unexpectedly"]);
  } finally {
    mock.restore();
  }
});

// ── documentsClient: URL builders ───────────────────────────────────────

test("preview URL builders encode variants and cache-busters", () => {
  assert.equal(
    documentsClient.documentThumbnailUrl("d1"),
    "/api/documents/d1/thumbnail",
  );
  assert.equal(
    documentsClient.documentPageUrl("d1", 2),
    "/api/documents/d1/pages/2?variant=full",
  );
  assert.equal(
    documentsClient.documentPageUrl("d1", 3, "thumb"),
    "/api/documents/d1/pages/3?variant=thumb",
  );
  // v carries the manifest's source_mtime so re-renders bust the cache.
  assert.equal(
    documentsClient.documentPageUrl("d1", 4, "full", 123),
    "/api/documents/d1/pages/4?variant=full&v=123",
  );
});

test("getDocumentPages() unwraps the page manifest", async () => {
  const mock = installFetch((url) => {
    assert.equal(url, "/api/documents/d1/pages");
    return jsonResponse({
      count: 3,
      width: 800,
      height: 600,
      source_mtime: 42,
      document_id: "d1",
      filename: "a.pdf",
    });
  });
  try {
    assert.deepEqual(await documentsClient.getDocumentPages("d1"), {
      count: 3,
      width: 800,
      height: 600,
      source_mtime: 42,
      document_id: "d1",
      filename: "a.pdf",
    });
  } finally {
    mock.restore();
  }

  const failing = installFetch(() => jsonResponse({}, 500));
  try {
    assert.equal(await documentsClient.getDocumentPages("d1"), null);
  } finally {
    failing.restore();
  }
});
