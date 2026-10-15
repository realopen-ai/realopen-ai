import assert from "node:assert/strict";
import test from "node:test";
import "./helpers/viteCompat.ts";
import type { ThinkingContext } from "../src/api/client.ts";

const { dispatchAgentEvent } = await import("../src/api/stream.ts");
const { useChatStore, dtoToMessage } =
  await import("../src/store/chatStore.ts");

test("coder thinking streams through ordinary blocks and retains its identity after reload", () => {
  useChatStore.setState({
    conversations: [
      {
        id: "qa",
        messages: [
          {
            id: "reply",
            role: "assistant",
            content: "",
            blocks: [],
            isStreaming: true,
          },
        ],
      },
    ],
  } as never);
  const current = () => useChatStore.getState().conversations[0].messages[0];
  const callbacks = {
    onToken() {},
    onThinkingStart(context?: ThinkingContext) {
      useChatStore.getState().startThinkingBlock("qa", "reply", context);
    },
    onThinkingToken(token: string) {
      useChatStore.getState().appendThinkingToken("qa", "reply", token);
    },
    onThinkingDone(seconds: number) {
      useChatStore.getState().finishThinkingBlock("qa", "reply", seconds);
    },
    onGenerationDone() {},
    onDone() {},
    onError() {},
  };
  const context = {
    agent: "coder",
    model: "ornith-1.5:9b",
    round: 2,
    parentId: "delegate-1",
  } as const;
  dispatchAgentEvent({ event: "thinking_start", ...context }, callbacks);
  dispatchAgentEvent(
    { event: "thinking", thinking: "Inspect existing routes." },
    callbacks,
  );
  assert.equal(current().blocks?.[0].duration, null);
  assert.equal(current().blocks?.[0].content, "Inspect existing routes.");
  assert.equal(current().blocks?.[0].agent, "coder");
  dispatchAgentEvent(
    { event: "thinking_done", thinkingDuration: 1.25 },
    callbacks,
  );
  dispatchAgentEvent({ event: "thinking_start" }, callbacks);
  dispatchAgentEvent(
    { event: "thinking", thinking: "General assistant resumes." },
    callbacks,
  );
  dispatchAgentEvent(
    { event: "thinking_done", thinkingDuration: 2 },
    callbacks,
  );
  assert.equal(current().blocks?.[1].agent, undefined);
  assert.equal(current().content, "");
  const restored = dtoToMessage({
    id: "reply",
    role: "assistant",
    content: "",
    blocks: current().blocks,
  } as never);
  assert.deepEqual(restored.blocks, current().blocks);
  assert.equal(restored.blocks?.[0].duration, 1.25);
  for (const [key, value] of Object.entries(context)) {
    assert.equal((restored.blocks?.[0] as any)[key], value);
  }
});
