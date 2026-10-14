import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import "./helpers/viteCompat.ts";
import type { AgentActivity } from "../src/store/chatStore.ts";

const { dispatchAgentEvent } = await import("../src/api/stream.ts");
const { useChatStore, dtoToMessage } =
  await import("../src/store/chatStore.ts");

function setup() {
  useChatStore.setState({
    currentConversationId: "other",
    conversations: [
      {
        id: "qa",
        messages: [
          {
            id: "reply",
            role: "assistant",
            content: "Partial answer",
            isStreaming: true,
            blocks: [],
          },
        ],
      },
      {
        id: "other",
        messages: [{ id: "untouched", role: "assistant", content: "Other" }],
      },
    ],
  } as never);
  return {
    onToken() {},
    onThinkingStart() {},
    onThinkingToken() {},
    onThinkingDone() {},
    onGenerationDone() {},
    onAgentActivity(activity: AgentActivity, active: boolean) {
      useChatStore.getState().setAgentActivity("qa", "reply", activity, active);
    },
    onDone() {
      useChatStore.getState().setStreaming("qa", "reply", false);
    },
    onInterrupted() {
      useChatStore.getState().setStreaming("qa", "reply", false);
    },
    onError() {
      useChatStore.getState().setStreaming("qa", "reply", false);
    },
  };
}

const current = () => useChatStore.getState().conversations[0].messages[0];

test("compaction and summary statuses show live, deduplicate replay and remain conversation-scoped", () => {
  const callbacks = setup();
  for (const activity of ["context_compaction", "conversation_summary"]) {
    const event = { event: "agent_activity", activity, active: true };
    dispatchAgentEvent(event, callbacks);
    dispatchAgentEvent(event, callbacks);
    assert.deepEqual(current().activeActivities, [activity]);
    assert.equal(current().isStreaming, true);
    assert.equal(current().content, "Partial answer");
    assert.equal(
      useChatStore.getState().conversations[1].messages[0].activeActivities,
      undefined,
    );
    dispatchAgentEvent({ ...event, active: false }, callbacks);
    assert.deepEqual(current().activeActivities, []);
  }
  dispatchAgentEvent(
    { event: "agent_activity", activity: "unknown", active: true },
    callbacks,
  );
  dispatchAgentEvent(
    { event: "agent_activity", activity: "context_compaction", active: "true" },
    callbacks,
  );
  assert.deepEqual(current().activeActivities, []);
});

test("queued background extraction is not shown as active summarization", () => {
  const callbacks = setup();
  dispatchAgentEvent({ event: "memory_extraction_start" }, callbacks);
  assert.deepEqual(current().activeActivities, ["memory_extraction"]);
  dispatchAgentEvent(
    { event: "memory_extraction_done", ran: true, pending: true, count: 0 },
    callbacks,
  );
  assert.deepEqual(current().activeActivities, []);
});

test("terminal states clear every transient activity without affecting the saved answer", () => {
  for (const terminal of ["interrupted", "error", "done"]) {
    const callbacks = setup();
    callbacks.onAgentActivity("context_compaction", true);
    callbacks.onAgentActivity("conversation_summary", true);
    if (terminal === "done") callbacks.onDone();
    else dispatchAgentEvent({ event: terminal, error: "Failed" }, callbacks);
    assert.deepEqual(current().activeActivities, []);
    assert.equal(current().content, "Partial answer");
    assert.equal(current().isStreaming, false);
  }
  const restored = dtoToMessage({
    id: "reply",
    role: "assistant",
    content: "Saved answer",
  } as never);
  assert.equal(restored.activeActivities, undefined);
});

test("activity indicators are accessible and translated in each locale", () => {
  const view = readFileSync(
    new URL("../src/components/chat/MessageBubble.tsx", import.meta.url),
    "utf8",
  );
  assert.match(view, /role="status"/);
  assert.match(view, /chat\.activity\.\$\{activity\}/);
  for (const locale of ["en", "fr", "ar"]) {
    const translations = JSON.parse(
      readFileSync(
        new URL(`../src/i18n/locales/${locale}.json`, import.meta.url),
        "utf8",
      ),
    );
    for (const activity of [
      "context_compaction",
      "conversation_summary",
      "memory_extraction",
    ]) {
      assert.ok(translations[`chat.activity.${activity}`]);
    }
  }
});
