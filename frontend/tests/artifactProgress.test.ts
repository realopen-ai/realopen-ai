import assert from "node:assert/strict";
import test from "node:test";
import "./helpers/viteCompat.ts";

const { dispatchAgentEvent } = await import("../src/api/stream.ts");

test("artifact SSE progress updates the existing running block without completing it", () => {
  const starts: unknown[] = [];
  const updates: unknown[] = [];
  const callbacks = {
    onToken() {},
    onThinkingStart() {},
    onThinkingToken() {},
    onThinkingDone() {},
    onGenerationDone() {},
    onDone() {
      throw new Error("Progress must not finish generation");
    },
    onError() {},
    onToolCallStart(call: unknown) {
      starts.push(call);
    },
    onToolCallUpdate(id: string, update: unknown) {
      updates.push({ id, update });
    },
  };
  dispatchAgentEvent(
    {
      event: "tool_call",
      tool_call: {
        id: "summary",
        type: "artifact",
        status: "running",
        title: "Summarize",
        startedAt: Date.now(),
      },
    },
    callbacks,
  );
  for (let completed = 0; completed <= 2; completed++) {
    dispatchAgentEvent(
      {
        event: "tool_call",
        tool_call: {
          id: "summary",
          progress: { stage: "batches", completed, total: 2 },
        },
      },
      callbacks,
    );
  }
  assert.equal(starts.length, 1);
  assert.equal(updates.length, 3);
  assert.deepEqual(updates[2], {
    id: "summary",
    update: { progress: { stage: "batches", completed: 2, total: 2 } },
  });
});
