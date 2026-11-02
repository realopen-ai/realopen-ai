import assert from "node:assert/strict";
import test from "node:test";
import "./helpers/viteCompat.ts";
import { safeWebUrl, webFetchPreview } from "../src/lib/webFetchPreview.ts";

const { dtoToMessage } = await import("../src/store/chatStore.ts");
const { dispatchAgentEvent } = await import("../src/api/stream.ts");

const content =
  "## Arrays\n\n```python\nprint(42)\n```\n\n| A | B |\n| --- | --- |\n| 1 | 2 |";
const output =
  "# Array guide\n\nSource: https://example.org/guide\nRequested URL: https://example.org/old\nAuthor: Alex Researcher\nPage date (metadata): 2025-02-12\nExtraction: markdownify\n\n" +
  content;

test("web fetch preview separates source metadata and preserves Markdown verbatim", () => {
  assert.deepEqual(webFetchPreview(output), {
    title: "Array guide",
    url: "https://example.org/guide",
    requestedUrl: "https://example.org/old",
    author: "Alex Researcher",
    date: "2025-02-12",
    content,
  });
});

test("legacy page text remains readable and unsafe source links are never clickable", () => {
  assert.equal(
    webFetchPreview(content, "https://example.org").content,
    content,
  );
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,bad",
    "/relative",
    "https://user:password@example.org",
  ]) {
    assert.equal(safeWebUrl(url), undefined);
    assert.equal(webFetchPreview("legacy", url).url, undefined);
  }
  assert.equal(webFetchPreview().content, "");
});

test("completed web fetch output survives both SSE and conversation reload", () => {
  const tc = {
    id: "fetch-call",
    type: "webfetch",
    status: "completed",
    title: "Read page: Array guide",
    output,
    query: "https://example.org/old",
  };
  let streamed: { output?: string; type?: string } | undefined;
  dispatchAgentEvent(
    { event: "tool_call", tool_call: tc },
    {
      onToken() {},
      onThinkingStart() {},
      onThinkingToken() {},
      onThinkingDone() {},
      onGenerationDone() {},
      onDone() {},
      onError() {},
      onToolCallStart(value) {
        streamed = value;
      },
      onToolCallUpdate(_id, value) {
        streamed = value;
      },
    },
  );
  assert.equal(streamed?.output, output);
  const reloaded = dtoToMessage({
    id: "assistant",
    conversation_id: "conversation",
    role: "assistant",
    content: "",
    created_at: "2026-11-02T10:00:00Z",
    blocks: [{ type: "tool_call", tool_call: tc }],
  });
  const saved = reloaded.blocks?.[0].toolCall;
  assert.equal(saved?.type, "webfetch");
  assert.equal(saved?.output, output);
  assert.deepEqual(
    webFetchPreview(saved?.output, saved?.query),
    webFetchPreview(output),
  );
});
