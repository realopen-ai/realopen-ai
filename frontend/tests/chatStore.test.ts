/**
 * Tests for src/store/chatStore.ts — the core conversation store.
 *
 * Direct import requires the alias hooks (`@/api/client`,
 * `@/store/toolCallPersistence`, … — see tests/helpers/viteCompat.ts); all
 * REST traffic is served by the globalThis.fetch mock. Two layers are
 * covered: the pure dtoToMessage() conversion (backend JSONB → frontend
 * Message/blocks/deliverables) and the store actions (conversation CRUD
 * with optimistic updates, block-based streaming assembly, digest progress
 * and deliverable bookkeeping).
 */
import assert from "node:assert/strict";
import test from "node:test";

import "./helpers/viteCompat.ts";
import { installFetch, jsonResponse } from "./helpers/fetchMock.ts";

const { dtoToMessage, useChatStore } =
  await import("../src/store/chatStore.ts");
import type { MessageDTO } from "../src/api/client.ts";

// ── Fixtures ────────────────────────────────────────────────────────────

function messageDTO(overrides: Partial<MessageDTO> = {}): MessageDTO {
  return {
    id: "m1",
    conversationId: "c1",
    role: "assistant",
    content: "hello",
    model: "qwen3:4b",
    tokens: null,
    hasImage: false,
    hasDocument: false,
    imageCount: 0,
    documentCount: 0,
    createdAt: 1_700_000_000_000,
    ...overrides,
  };
}

function conversation(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    title: `Conversation ${id}`,
    messages: [],
    model: "qwen3:4b",
    createdAt: 1,
    updatedAt: 1,
    pinned: false,
    archived: false,
    pinnedAt: null,
    archivedAt: null,
    ...overrides,
  };
}

function reset(overrides: Record<string, unknown> = {}) {
  useChatStore.setState({
    conversations: [],
    activeConversationId: null,
    models: [],
    selectedModel: "default",
    profileName: "",
    profileLabel: "",
    isStreaming: false,
    isLoadingConversations: false,
    modules: [],
    isLoadingModules: false,
    ...overrides,
  });
}

function activeMessages() {
  return useChatStore.getState().getActiveMessages();
}

// ── dtoToMessage: pure backend → frontend conversion ───────────────────

test("dtoToMessage maps plain thinking/text blocks with stable ids", () => {
  const message = dtoToMessage(
    messageDTO({
      blocks: [
        { type: "thinking", content: "hmm", duration: 2 },
        { type: "text", content: "answer" },
        { type: "error", content: "boom" },
      ],
    }),
  );

  assert.deepEqual(
    message.blocks?.map((b) => [b.id, b.type, b.content]),
    [
      ["block-m1-0", "thinking", "hmm"],
      ["block-m1-1", "text", "answer"],
      ["block-m1-2", "error", "boom"],
    ],
  );
  assert.equal(message.blocks?.[0].duration, 2);
});

test("dtoToMessage fills tool_call defaults for legacy JSONB rows", () => {
  const message = dtoToMessage(
    messageDTO({
      blocks: [{ type: "tool_call", tool_call: {} }],
    }),
  );
  const toolCall = message.blocks?.[0].toolCall!;

  // Everything missing falls back instead of crashing the renderer.
  assert.equal(toolCall.id, "tc-m1-0");
  assert.equal(toolCall.type, "websearch");
  assert.equal(toolCall.status, "completed");
  assert.equal(toolCall.title, "Tool"); // falls back to the raw type
  assert.equal(toolCall.startedAt, 1_700_000_000_000); // dto.createdAt
  assert.equal(toolCall.completedAt, undefined);
});

test("dtoToMessage normalizes legacy second timestamps and keeps details", () => {
  const message = dtoToMessage(
    messageDTO({
      createdAt: 1_700_000_000, // seconds
      blocks: [
        {
          type: "tool_call",
          tool_call: {
            id: "tc-9",
            type: "code_exec",
            status: "error",
            title: "Run code",
            startedAt: 1_700_000_000, // legacy seconds → ms
            completedAt: 1_700_000_002,
            durationMs: 2000,
            language: "python",
            code: "print('hi')",
            output: "hi",
            exitCode: 0,
            // nested coder details must survive the round-trip
            filePath: "/app/main.py",
            fileContent: "print('hi')",
            diff: "+print('hi')",
            previewUrl: "/api/sandboxes/s1/preview/8000/",
            previewPort: 8000,
            sandboxId: "s1",
            sandbox: { id: "s1", name: "w", status: "running" },
            unrelated: "dropped",
          },
        },
      ],
    }),
  );
  const toolCall = message.blocks?.[0].toolCall!;

  assert.equal(toolCall.startedAt, 1_700_000_000_000);
  assert.equal(toolCall.completedAt, 1_700_000_002_000);
  assert.equal(toolCall.exitCode, 0);
  assert.equal(toolCall.filePath, "/app/main.py");
  assert.equal(toolCall.previewPort, 8000);
  assert.deepEqual(toolCall.sandbox, {
    id: "s1",
    name: "w",
    status: "running",
  });
  assert.equal("unrelated" in toolCall, false);
});

test("dtoToMessage derives streaming state and completion defaults", () => {
  const streaming = dtoToMessage(messageDTO({ completionStatus: "streaming" }));
  assert.equal(streaming.isStreaming, true);
  assert.equal(streaming.completionStatus, "streaming");

  const legacy = dtoToMessage(messageDTO({ role: "user", blocks: null }));
  assert.equal(legacy.isStreaming, false);
  assert.equal(legacy.completionStatus, "completed");
  assert.equal(legacy.blocks, undefined);
});

test("dtoToMessage backfills pptx report thumbnails", () => {
  const message = dtoToMessage(
    messageDTO({
      deliverables: [
        {
          type: "report",
          format: "pdf",
          filename: "a.pdf",
          file_path: "/a",
          download_url: "/api/reports/1/file",
        },
        {
          type: "report",
          format: "pptx",
          filename: "b.pptx",
          file_path: "/b",
          download_url: "/api/reports/2/file",
          report_id: "r2",
        },
        {
          type: "report",
          format: "pptx",
          filename: "c.pptx",
          file_path: "/c",
          download_url: "/api/reports/3/file",
          report_id: "r3",
          thumbnail_url: "/custom.png",
        },
      ],
    }),
  );

  const [pdf, pptx, custom] = message.deliverables!;
  assert.equal(pdf.thumbnail_url, undefined); // only pptx gets the fallback
  assert.equal(pptx.thumbnail_url, "/api/reports/r2/thumbnail");
  assert.equal(custom.thumbnail_url, "/custom.png"); // explicit wins
});

// ── Conversation list / CRUD ────────────────────────────────────────────

test("loadConversations() fetches active + archived lists and preserves loaded messages", async () => {
  reset({
    conversations: [
      conversation("keep", {
        messages: [dtoToMessage(messageDTO({ id: "kept" }))],
      }),
    ],
  });
  const urls: string[] = [];
  const mock = installFetch((url) => {
    urls.push(url);
    if (url === "/api/conversations?limit=50&offset=0&archived=false") {
      return jsonResponse({
        conversations: [
          {
            id: "keep",
            title: "Keep",
            model: null,
            createdAt: 5,
            updatedAt: 5,
          },
          {
            id: "fresh",
            title: null,
            model: null,
            createdAt: 6,
            updatedAt: 6,
            pinned: true,
          },
        ],
      });
    }
    if (url === "/api/conversations?limit=100&offset=0&archived=true") {
      return jsonResponse({
        conversations: [
          {
            id: "arch",
            title: "Archived",
            model: null,
            createdAt: 4,
            updatedAt: 4,
            archived: true,
          },
        ],
      });
    }
    throw new Error(`Unexpected fetch in test: ${url}`);
  });
  try {
    await useChatStore.getState().loadConversations();

    const state = useChatStore.getState();
    assert.equal(state.isLoadingConversations, false);
    assert.deepEqual(
      state.conversations.map((c) => c.id),
      ["keep", "fresh", "arch"],
    );
    // Defaults applied for missing backend fields.
    assert.equal(state.conversations[1].title, "New Chat");
    assert.equal(state.conversations[1].model, "default");
    assert.equal(state.conversations[1].pinned, true);
    // Already-loaded messages survive the refresh.
    assert.equal(state.conversations[0].messages.length, 1);
    assert.equal(state.conversations[0].messages[0].id, "kept");
    // Fresh conversations start with lazy (empty) messages.
    assert.equal(state.conversations[1].messages.length, 0);
  } finally {
    mock.restore();
  }
});

test("createConversation() prefers the backend and falls back to a local id", async () => {
  reset({ selectedModel: "qwen3:4b" });
  let backendWorks = true;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/conversations?title=New+Chat&model=qwen3%3A4b");
    assert.equal(init?.method, "POST");
    if (!backendWorks) return jsonResponse({}, 500);
    return jsonResponse({
      id: "from-backend",
      title: "New Chat",
      model: "qwen3:4b",
      createdAt: 1,
      updatedAt: 1,
    });
  });
  try {
    const backendId = await useChatStore.getState().createConversation();
    assert.equal(backendId, "from-backend");
    assert.equal(useChatStore.getState().activeConversationId, "from-backend");

    backendWorks = false;
    const localId = await useChatStore.getState().createConversation();
    assert.match(localId, /^local-/);
    assert.equal(useChatStore.getState().activeConversationId, localId);
    assert.equal(useChatStore.getState().conversations.length, 2);
    assert.equal(useChatStore.getState().conversations[0].id, localId);
  } finally {
    mock.restore();
  }
});

test("deleteConversation() removes optimistically and clears the active id", async () => {
  reset({
    conversations: [conversation("a"), conversation("b")],
    activeConversationId: "a",
  });
  const urls: string[] = [];
  const mock = installFetch((url, init) => {
    urls.push(`${init?.method} ${url}`);
    return jsonResponse({}, 200);
  });
  try {
    await useChatStore.getState().deleteConversation("a");
    assert.deepEqual(
      useChatStore.getState().conversations.map((c) => c.id),
      ["b"],
    );
    assert.equal(useChatStore.getState().activeConversationId, null);
    assert.deepEqual(urls, ["DELETE /api/conversations/a"]);

    // Deleting a non-active conversation leaves the active id alone.
    useChatStore.setState({ activeConversationId: "b" });
    await useChatStore.getState().deleteConversation("ghost");
    assert.equal(useChatStore.getState().activeConversationId, "b");
  } finally {
    mock.restore();
  }
});

test("setActiveConversation() lazily loads messages once", async () => {
  reset({ conversations: [conversation("c1")] });
  let fetches = 0;
  const mock = installFetch((url) => {
    fetches += 1;
    assert.equal(url, "/api/conversations/c1");
    return jsonResponse({
      messages: [
        messageDTO({ id: "m1" }),
        messageDTO({ id: "m2", role: "user" }),
      ],
    });
  });
  try {
    useChatStore.getState().setActiveConversation("c1");
    // The load is kicked off but awaited internally; flush the microtasks.
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(fetches, 1);
    assert.deepEqual(
      activeMessages().map((m) => m.id),
      ["m1", "m2"],
    );

    // Re-selecting does not reload — messages are already in the store.
    useChatStore.getState().setActiveConversation("c1");
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(fetches, 1);
  } finally {
    mock.restore();
  }
});

test("loadConversationById() reports 404 and adopts found conversations", async () => {
  reset({
    conversations: [
      conversation("loaded", { messages: [dtoToMessage(messageDTO())] }),
    ],
  });
  const mock = installFetch((url) => {
    if (url === "/api/conversations/missing")
      return jsonResponse({ detail: "not found" }, 404);
    if (url === "/api/conversations/new") {
      return jsonResponse({
        conversation: {
          id: "new",
          title: "New",
          model: null,
          createdAt: 1,
          updatedAt: 1,
        },
        messages: [messageDTO({ id: "fresh-1" })],
      });
    }
    if (url === "/api/conversations/loaded") {
      return jsonResponse({
        conversation: {
          id: "loaded",
          title: "Loaded",
          model: null,
          createdAt: 1,
          updatedAt: 1,
        },
        messages: [messageDTO({ id: "server-1" })],
      });
    }
    throw new Error(`Unexpected fetch in test: ${url}`);
  });
  try {
    assert.equal(
      await useChatStore.getState().loadConversationById("missing"),
      false,
    );
    assert.equal(useChatStore.getState().conversations.length, 1);

    assert.equal(
      await useChatStore.getState().loadConversationById("new"),
      true,
    );
    assert.equal(useChatStore.getState().activeConversationId, "new");
    assert.deepEqual(
      useChatStore.getState().conversations.map((c) => c.id),
      ["new", "loaded"],
    );
    assert.equal(useChatStore.getState().conversations[0].messages.length, 1);

    // Direct URL access to an already-loaded conversation keeps its
    // messages (they may be mid-stream).
    assert.equal(
      await useChatStore.getState().loadConversationById("loaded"),
      true,
    );
    const loaded = useChatStore
      .getState()
      .conversations.find((c) => c.id === "loaded")!;
    assert.equal(loaded.messages[0].id, "m1");
  } finally {
    mock.restore();
  }
});

// ── Message + title/flag mutations ──────────────────────────────────────

test("addMessage() defaults blocks for assistant messages only", () => {
  reset({ conversations: [conversation("c1")], activeConversationId: "c1" });

  const userMsgId = useChatStore.getState().addMessage("c1", {
    role: "user",
    content: "hi",
  });
  const assistantMsgId = useChatStore.getState().addMessage("c1", {
    role: "assistant",
    content: "",
  });

  const messages = activeMessages();
  assert.equal(messages.length, 2);
  assert.match(userMsgId, /^msg-/);
  assert.match(assistantMsgId, /^msg-/);
  assert.equal(messages[0].blocks, undefined);
  assert.deepEqual(messages[1].blocks, []);
  assert.equal(messages[1].isStreaming, false);
  assert.ok(messages[1].createdAt > 0);
});

test("updateMessage() patches only the targeted message", () => {
  reset({ conversations: [conversation("c1")], activeConversationId: "c1" });
  const id = useChatStore.getState().addMessage("c1", {
    role: "assistant",
    content: "",
  });
  useChatStore.getState().addMessage("c1", { role: "user", content: "other" });

  useChatStore.getState().updateMessage("c1", id, {
    content: "done",
    completionStatus: "completed",
  });

  assert.equal(activeMessages()[0].content, "done");
  assert.equal(activeMessages()[0].completionStatus, "completed");
  assert.equal(activeMessages()[1].content, "other");
});

test("setConversationTitle() trims and ignores empty titles", () => {
  reset({ conversations: [conversation("c1", { title: "Before" })] });

  useChatStore.getState().setConversationTitle("c1", "  After  ");
  assert.equal(useChatStore.getState().conversations[0].title, "After");

  useChatStore.getState().setConversationTitle("c1", "   ");
  assert.equal(useChatStore.getState().conversations[0].title, "After");
});

test("renameConversation() updates optimistically and reverts on failure", async () => {
  reset({ conversations: [conversation("c1", { title: "Before" })] });
  let ok = true;
  const mock = installFetch((url, init) => {
    assert.equal(init?.method, "PATCH");
    assert.equal(url, "/api/conversations/c1?title=After");
    return jsonResponse({}, ok ? 200 : 500);
  });
  try {
    // Same title → no request at all.
    await useChatStore.getState().renameConversation("c1", "Before");
    assert.equal(mock.calls.length, 0);

    await useChatStore.getState().renameConversation("c1", "After");
    assert.equal(useChatStore.getState().conversations[0].title, "After");

    // A failed rename reverts to the title the user still sees.
    ok = false;
    await useChatStore.getState().renameConversation("c1", "Final");
    assert.equal(useChatStore.getState().conversations[0].title, "After");
  } finally {
    mock.restore();
  }
});

test("togglePinConversation() pins-and-unarchives, reverts on failure", async () => {
  reset({
    conversations: [conversation("c1", { archived: true, archivedAt: 9 })],
  });
  let ok = true;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/conversations/c1?pinned=true");
    assert.equal(init?.method, "PATCH");
    return jsonResponse({}, ok ? 200 : 500);
  });
  try {
    await useChatStore.getState().togglePinConversation("c1");
    let conv = useChatStore.getState().conversations[0];
    assert.equal(conv.pinned, true);
    assert.ok(conv.pinnedAt !== null);
    assert.equal(conv.archived, false); // pin implies unarchive
    assert.equal(conv.archivedAt, null);

    // Failed unpin reverts to the previous flags.
    ok = false;
    await useChatStore.getState().togglePinConversation("c1");
    conv = useChatStore.getState().conversations[0];
    assert.equal(conv.pinned, true);
    assert.equal(conv.archived, false);
  } finally {
    mock.restore();
  }
});

test("toggleArchiveConversation() archives-and-unpins, reverts on failure", async () => {
  reset({
    conversations: [conversation("c1", { pinned: true, pinnedAt: 9 })],
  });
  let ok = true;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/conversations/c1?archived=true");
    assert.equal(init?.method, "PATCH");
    return jsonResponse({}, ok ? 200 : 500);
  });
  try {
    await useChatStore.getState().toggleArchiveConversation("c1");
    let conv = useChatStore.getState().conversations[0];
    assert.equal(conv.archived, true);
    assert.ok(conv.archivedAt !== null);
    assert.equal(conv.pinned, false); // archive implies unpin
    assert.equal(conv.pinnedAt, null);

    ok = false;
    await useChatStore.getState().toggleArchiveConversation("c1");
    conv = useChatStore.getState().conversations[0];
    assert.equal(conv.archived, true);
    assert.equal(conv.pinned, false);
  } finally {
    mock.restore();
  }
});

// ── Block-based streaming assembly ──────────────────────────────────────

function seededAssistant() {
  reset({ conversations: [conversation("c1")], activeConversationId: "c1" });
  const messageId = useChatStore.getState().addMessage("c1", {
    role: "assistant",
    content: "",
    isStreaming: false,
    completionStatus: "streaming",
  } as never);
  return messageId;
}

test("thinking blocks accumulate tokens and finalize with a duration", () => {
  const messageId = seededAssistant();
  const store = useChatStore.getState();

  store.startThinkingBlock("c1", messageId);
  store.appendThinkingToken("c1", messageId, "let me ");
  store.appendThinkingToken("c1", messageId, "think");
  store.finishThinkingBlock("c1", messageId, 3);

  const blocks = activeMessages()[0].blocks!;
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "thinking");
  assert.equal(blocks[0].id, `block-${messageId}-0`);
  assert.equal(blocks[0].content, "let me think");
  assert.equal(blocks[0].duration, 3);
});

test("thinking tokens are ignored once another block type is appended", () => {
  const messageId = seededAssistant();
  const store = useChatStore.getState();

  store.startThinkingBlock("c1", messageId);
  store.appendTextToken("c1", messageId, "answer");
  // A late thinking token after the text block must not corrupt the text.
  store.appendThinkingToken("c1", messageId, "stray");
  store.finishThinkingBlock("c1", messageId, 1);

  const blocks = activeMessages()[0].blocks!;
  assert.deepEqual(
    blocks.map((b) => [b.type, b.content]),
    [
      ["thinking", ""],
      ["text", "answer"],
    ],
  );
  // finishThinkingBlock on a non-thinking tail is a no-op too.
  assert.equal(blocks[blocks.length - 1].duration, undefined);
});

test("text tokens merge into the trailing text block and mirror into content", () => {
  const messageId = seededAssistant();
  const store = useChatStore.getState();

  store.startThinkingBlock("c1", messageId);
  store.appendTextToken("c1", messageId, "Hello ");
  store.appendTextToken("c1", messageId, "world");
  store.startToolCallBlock("c1", messageId, {
    id: "tc-1",
    type: "websearch",
    status: "running",
    title: "Search",
    startedAt: 1,
  });
  // Text after a tool call opens a NEW text block (chronological flow).
  store.appendTextToken("c1", messageId, "After");

  const message = activeMessages()[0];
  assert.deepEqual(
    message.blocks?.map((b) => b.type),
    ["thinking", "text", "tool_call", "text"],
  );
  assert.equal(message.blocks?.[1].content, "Hello world");
  assert.equal(message.blocks?.[3].content, "After");
  // content mirrors all text tokens for plain-text consumers.
  assert.equal(message.content, "Hello worldAfter");
});

test("tool call blocks update by tool call id and accept RAG sources", () => {
  const messageId = seededAssistant();
  const store = useChatStore.getState();

  store.startToolCallBlock("c1", messageId, {
    id: "tc-1",
    type: "rag_search",
    status: "running",
    title: "Search knowledge",
    startedAt: 1,
  });
  store.startToolCallBlock("c1", messageId, {
    id: "tc-2",
    type: "code_exec",
    status: "running",
    title: "Run code",
    startedAt: 2,
  });

  store.updateToolCallBlock("c1", messageId, "tc-1", {
    status: "completed",
    completedAt: 5,
    durationMs: 4,
  });
  store.setToolCallSources("c1", messageId, "tc-1", [
    {
      document_id: "d1",
      document_filename: "doc.pdf",
      chunk_id: "ch1",
      text: "text",
      snippet: "snip",
      page_number: 1,
      line_start: null,
      line_end: null,
      chunk_type: "text",
      score: 0.9,
      vector_sim: 0.8,
      bm25_score: 1.2,
    },
  ]);

  const blocks = activeMessages()[0].blocks!;
  const first = blocks.find((b) => b.toolCall?.id === "tc-1")!.toolCall!;
  const second = blocks.find((b) => b.toolCall?.id === "tc-2")!.toolCall!;
  assert.equal(first.status, "completed");
  assert.equal(first.durationMs, 4);
  assert.equal(first.sources?.length, 1);
  assert.equal(second.status, "running"); // untouched
  assert.equal(second.sources, undefined);
});

test("setGenerationDuration() accumulates across agent rounds", () => {
  const messageId = seededAssistant();
  const store = useChatStore.getState();

  store.setGenerationDuration("c1", messageId, 2);
  store.setGenerationDuration("c1", messageId, 3);

  assert.equal(activeMessages()[0].generationDuration, 5);
});

test("setStreaming(false) stamps completedAt and clears the global flag", () => {
  const messageId = seededAssistant();
  useChatStore.setState({ isStreaming: true });
  useChatStore.getState().setStreaming("c1", messageId, false);

  const message = activeMessages()[0];
  assert.equal(message.isStreaming, false);
  assert.ok(message.completedAt !== undefined);
  assert.equal(useChatStore.getState().isStreaming, false);
});

test("addDigestProgress() dedupes identical re-delivered events", () => {
  const messageId = seededAssistant();
  const store = useChatStore.getState();
  const item = (filename: string, stage: string, percent: number) => ({
    filename,
    stage,
    percent,
    details: stage,
  });

  // The dedupe key is filename+stage+percent — an SSE event replayed
  // verbatim replaces its earlier copy instead of stacking up.
  store.addDigestProgress(
    "c1",
    messageId,
    item("a.pdf", "extracting_text", 10),
  );
  store.addDigestProgress(
    "c1",
    messageId,
    item("a.pdf", "extracting_text", 10),
  );
  // A changed percent is a NEW progress step, not a replacement.
  store.addDigestProgress(
    "c1",
    messageId,
    item("a.pdf", "extracting_text", 20),
  );
  store.addDigestProgress("c1", messageId, item("b.pdf", "chunking", 50));

  const progress = activeMessages()[0].digestProgress!;
  assert.deepEqual(
    progress.map((p) => [p.filename, p.stage, p.percent]),
    [
      ["a.pdf", "extracting_text", 10],
      ["a.pdf", "extracting_text", 20],
      ["b.pdf", "chunking", 50],
    ],
  );
});

test("addDeliverables() dedupes by download_url and backfills pptx thumbnails", () => {
  const messageId = seededAssistant();
  const store = useChatStore.getState();
  const report = {
    type: "report",
    format: "docx",
    filename: "r.docx",
    file_path: "/r",
    download_url: "/api/reports/1/file",
  };

  store.addDeliverables("c1", messageId, [report]);
  store.addDeliverables("c1", messageId, [
    report, // duplicate download_url → ignored
    {
      type: "report",
      format: "pptx",
      filename: "r.pptx",
      file_path: "/r2",
      download_url: "/api/reports/2/file",
      report_id: "r2",
    },
  ]);

  const deliverables = activeMessages()[0].deliverables!;
  assert.equal(deliverables.length, 2);
  assert.equal(deliverables[0].format, "docx");
  assert.equal(deliverables[1].thumbnail_url, "/api/reports/r2/thumbnail");
});

// ── Modules ─────────────────────────────────────────────────────────────

test("loadModules() replaces the module list", async () => {
  reset();
  const mock = installFetch((url) => {
    assert.equal(url, "/api/modules");
    return jsonResponse({
      modules: [
        {
          name: "assistant",
          required: true,
          enabled: true,
          label: "Assistant",
          description: "",
          icon: "sparkles",
          available: true,
          models_downloaded: true,
          can_toggle: false,
          requirements_met: true,
        },
      ],
    });
  });
  try {
    await useChatStore.getState().loadModules();
    assert.equal(useChatStore.getState().modules.length, 1);
    assert.equal(useChatStore.getState().isLoadingModules, false);
  } finally {
    mock.restore();
  }
});

test("toggleModule() updates the row, reloads models, and reports failure", async () => {
  reset({
    modules: [
      {
        name: "vision",
        required: false,
        enabled: false,
        label: "Vision",
        description: "",
        icon: "eye",
        available: true,
        models_downloaded: false,
        can_toggle: true,
        requirements_met: true,
      },
    ],
  });
  let ok = true;
  const urls: string[] = [];
  const mock = installFetch((url, init) => {
    urls.push(url);
    if (url === "/api/modules/toggle") {
      assert.equal(init?.method, "POST");
      assert.deepEqual(JSON.parse(String(init?.body)), {
        module: "vision",
        enabled: true,
      });
      return ok
        ? jsonResponse({
            module: "vision",
            enabled: true,
            models_downloaded: true,
          })
        : jsonResponse({}, 500);
    }
    if (url === "/api/profile/models") {
      return jsonResponse({
        profile: "cpu_small",
        label: "CPU",
        models: [
          {
            id: "moondream:1.8b",
            type: "vision",
            role: "default_vision",
            description: "",
            size: "1.7 GB",
          },
        ],
      });
    }
    throw new Error(`Unexpected fetch in test: ${url}`);
  });
  try {
    assert.equal(
      await useChatStore.getState().toggleModule("vision", true),
      true,
    );
    assert.equal(useChatStore.getState().modules[0].enabled, true);
    assert.equal(useChatStore.getState().modules[0].models_downloaded, true);
    // The model dropdown was refreshed to include the new module's models.
    assert.deepEqual(
      useChatStore.getState().models.map((m) => m.id),
      ["moondream:1.8b"],
    );

    ok = false;
    assert.equal(
      await useChatStore.getState().toggleModule("vision", false),
      false,
    );
    assert.deepEqual(urls, [
      "/api/modules/toggle",
      "/api/profile/models",
      "/api/modules/toggle",
    ]);
  } finally {
    mock.restore();
  }
});

test("getActiveConversation() follows the active id", () => {
  reset({
    conversations: [conversation("a"), conversation("b")],
    activeConversationId: "b",
  });
  assert.equal(useChatStore.getState().getActiveConversation()?.id, "b");

  useChatStore.setState({ activeConversationId: "ghost" });
  assert.equal(useChatStore.getState().getActiveConversation(), undefined);
  assert.deepEqual(useChatStore.getState().getActiveMessages(), []);
});
