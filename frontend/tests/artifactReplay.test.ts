import assert from "node:assert/strict";
import test from "node:test";
import "./helpers/viteCompat.ts";

const { useChatStore } = await import("../src/store/chatStore.ts");
const { resumeChatStream } = await import("../src/api/stream.ts");

test("concurrent reconnects share one SSE subscription", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  let release: (response: Response) => void = () => {};
  globalThis.fetch = (() => {
    calls++;
    return new Promise<Response>((resolve) => {
      release = resolve;
    });
  }) as typeof fetch;
  try {
    const connected = () => {
      throw new Error("404 must not attach callbacks");
    };
    const first = resumeChatStream("qa-reconnect", connected);
    const second = resumeChatStream("qa-reconnect", connected);
    assert.equal(first, second);
    assert.equal(calls, 1);
    release(new Response(null, { status: 404 }));
    assert.equal(await first, false);
  } finally {
    globalThis.fetch = original;
  }
});

test("replayed tool start leaves persisted progress and single block intact", () => {
  const toolCall = {
    id: "summary",
    type: "artifact",
    title: "Summarize",
    status: "running",
    startedAt: 1,
    progress: { stage: "batches", completed: 8, total: 17 },
  };
  useChatStore.setState({
    conversations: [
      {
        id: "qa",
        messages: [
          {
            id: "reply",
            role: "assistant",
            content: "",
            blocks: [{ id: "block", type: "tool_call", toolCall }],
          },
        ],
      },
    ],
  } as never);
  useChatStore.getState().startToolCallBlock("qa", "reply", {
    ...toolCall,
    progress: undefined,
  } as never);
  const blocks = useChatStore.getState().conversations[0].messages[0].blocks!;
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].toolCall?.progress?.completed, 8);
});
