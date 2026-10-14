import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import "./helpers/viteCompat.ts";
import { toolOutputDetails } from "../src/lib/toolOutputDetails.ts";

const { dispatchAgentEvent } = await import("../src/api/stream.ts");
const { dtoToMessage } = await import("../src/store/chatStore.ts");

const page = {
  result_ref: "call-original",
  content: "🙂ab",
  offset: 2,
  next_offset: 5,
  total_chars: 10,
  source_tool: "rag_search",
  source_title: "Document search",
};

test("output paging details use Unicode character ranges and exact source metadata", () => {
  const details = toolOutputDetails(JSON.stringify(page))!;
  assert.equal(details.offset, 2);
  assert.equal(details.end, 5);
  assert.equal(details.count, 3);
  assert.equal(details.nextOffset, 5);
  assert.equal(details.sourceTool, "rag_search");
  assert.equal(details.sourceTitle, "Document search");
  assert.equal(details.formatted, page.content);
  assert.equal(details.isJson, false);
});

test("complete JSON is readable while partial pages and empty results stay exact", () => {
  const content = '{"title":"Example","rows":[1,2]}';
  const full = toolOutputDetails(
    JSON.stringify({
      ...page,
      content,
      offset: 0,
      next_offset: null,
      total_chars: content.length,
    }),
  )!;
  assert.equal(full.isJson, true);
  assert.equal(full.formatted, JSON.stringify(JSON.parse(content), null, 2));
  assert.equal(full.end, content.length);
  const partial = toolOutputDetails(
    JSON.stringify({
      ...page,
      content: '{"title":',
      offset: 0,
      next_offset: 9,
    }),
  )!;
  assert.equal(partial.formatted, '{"title":');
  assert.equal(partial.isJson, false);
  const empty = toolOutputDetails(
    JSON.stringify({
      result_ref: "ref",
      content: "",
      offset: 0,
      next_offset: null,
      total_chars: 0,
    }),
  )!;
  assert.equal(empty.count, 0);
  assert.equal(empty.sourceTool, undefined);
});

test("malformed paging values do not invent ranges or tool names", () => {
  for (const bad of [
    undefined,
    "broken",
    "[]",
    "null",
    JSON.stringify({ ...page, offset: -1 }),
    JSON.stringify({ ...page, offset: "2" }),
    JSON.stringify({ ...page, total_chars: 1 }),
    JSON.stringify({ ...page, next_offset: null }),
    JSON.stringify({ ...page, next_offset: 9 }),
    JSON.stringify({ ...page, content: null }),
  ]) {
    assert.equal(toolOutputDetails(bad), null);
  }
  const legacy = {
    result_ref: "old",
    content: "text",
    offset: 0,
    next_offset: null,
    total_chars: 4,
  };
  assert.equal(toolOutputDetails(JSON.stringify(legacy))?.formatted, "text");
  assert.equal(
    toolOutputDetails(JSON.stringify(legacy))?.sourceTool,
    undefined,
  );
});

test("output details survive live updates and persisted message restoration", () => {
  const calls = new Map();
  const output = JSON.stringify(page);
  const callbacks = {
    onToken() {},
    onThinkingStart() {},
    onThinkingToken() {},
    onThinkingDone() {},
    onGenerationDone() {},
    onDone() {},
    onError() {},
    onToolCallStart(call: { id: string }) {
      calls.set(call.id, { ...calls.get(call.id), ...call });
    },
    onToolCallUpdate(id: string, update: object) {
      Object.assign(calls.get(id), update);
    },
  };
  const tc = {
    id: "reader",
    type: "tool_output",
    status: "completed",
    title: "Read tool result",
    output,
    startedAt: 1700000000000,
    completedAt: 1700000000001,
  };
  dispatchAgentEvent({ event: "tool_call", tool_call: tc }, callbacks);
  const restored = dtoToMessage({
    id: "reply",
    role: "assistant",
    content: "",
    blocks: [{ type: "tool_call", tool_call: tc }],
  } as never);
  assert.deepEqual(
    toolOutputDetails(calls.get("reader").output),
    toolOutputDetails(restored.blocks?.[0].toolCall?.output),
  );
});

test("tool output details are collapsed by default, highlight JSON and have all locales", () => {
  const view = readFileSync(
    new URL("../src/components/chat/MessageBubble.tsx", import.meta.url),
    "utf8",
  );
  assert.match(view, /tc\.type !== "tool_output" &&/);
  assert.match(view, /<ToolOutputDetail tc=\{tc\}/);
  assert.match(
    view,
    /<HighlightedCode code=\{page\.formatted\} language="json"/,
  );
  assert.match(view, /aria-expanded=\{isExpanded\}/);
  for (const locale of ["en", "fr", "ar"]) {
    const translations = JSON.parse(
      readFileSync(
        new URL(`../src/i18n/locales/${locale}.json`, import.meta.url),
        "utf8",
      ),
    );
    for (const key of [
      "source",
      "reference",
      "range",
      "more",
      "nextOffset",
      "end",
      "content",
      "empty",
      "loadingInfo",
      "unavailable",
    ]) {
      assert.ok(translations[`tool.output.${key}`]);
    }
  }
});
