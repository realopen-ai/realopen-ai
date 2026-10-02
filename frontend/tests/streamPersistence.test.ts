import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const streamApi = readFileSync(
  new URL("../src/api/stream.ts", import.meta.url),
  "utf8",
);
const chatArea = readFileSync(
  new URL("../src/components/chat/ChatArea.tsx", import.meta.url),
  "utf8",
);
const bubble = readFileSync(
  new URL("../src/components/chat/MessageBubble.tsx", import.meta.url),
  "utf8",
);

test("user stop targets the durable backend stream", () => {
  assert.match(streamApi, /chat\/stream\/\$\{encodeURIComponent\(conversationId\)\}\/stop/);
  assert.match(streamApi, /callbacks\.onInterrupted\?\.\(\)/);
  assert.match(chatArea, /completionStatus: "interrupted"/);
});

test("reload reconnects to buffered SSE and rebuilds the live message", () => {
  assert.match(streamApi, /export async function resumeChatStream/);
  assert.match(streamApi, /chat\/stream\/\$\{encodeURIComponent\(conversationId\)\}\/events/);
  assert.match(chatArea, /resumeChatStream\(urlConvId/);
  assert.match(chatArea, /Keep the DB-persisted Markdown blocks/);
  assert.doesNotMatch(chatArea, /content: "",\s*blocks: \[\],\s*deliverables: \[\]/);
  assert.match(chatArea, /current\?\.model === "external" \? undefined/);
});

test("normal navigation after completion does not create a reconnect bubble", () => {
  assert.match(chatArea, /const hasStreamingAssistant = hydratedMessages\.some/);
  assert.match(
    chatArea,
    /if \(!hasStreamingAssistant && latestHydratedMessage\?\.role !== "user"\)/,
  );
});

test("interrupted assistant messages render a durable badge", () => {
  assert.match(bubble, /message\.completionStatus === "interrupted"/);
  assert.match(bubble, /message\.interrupted/);
});
