import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("skill loading has a dedicated tool-call presentation", () => {
  const store = readFileSync(
    new URL("../src/store/chatStore.ts", import.meta.url),
    "utf8",
  );
  const message = readFileSync(
    new URL("../src/components/chat/MessageBubble.tsx", import.meta.url),
    "utf8",
  );

  assert.match(store, /\| "skill"/);
  assert.match(message, /skill:\s*\{[\s\S]*label: "Loaded skill"/);
  assert.match(message, /runningLabel: "Loading skill"/);
});
