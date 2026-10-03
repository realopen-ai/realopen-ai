import assert from "node:assert/strict";
import test from "node:test";

import { isConversationStreaming } from "../src/lib/conversationActivity.ts";

test("detects text and voice assistant messages that are still streaming", () => {
  assert.equal(
    isConversationStreaming({
      messages: [
        {
          id: "assistant-text",
          role: "assistant",
          content: "",
          isStreaming: true,
          createdAt: 1,
        },
      ],
    }),
    true,
  );

  assert.equal(
    isConversationStreaming({
      messages: [
        {
          id: "assistant-voice",
          role: "assistant",
          content: "Hello",
          modality: "voice",
          isStreaming: true,
          createdAt: 2,
        },
      ],
    }),
    true,
  );
});

test("stops indicating activity after every message is finalized", () => {
  assert.equal(
    isConversationStreaming({
      messages: [
        {
          id: "assistant-done",
          role: "assistant",
          content: "Done",
          isStreaming: false,
          createdAt: 1,
        },
      ],
    }),
    false,
  );
});
