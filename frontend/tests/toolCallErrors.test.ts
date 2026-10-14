import assert from "node:assert/strict";
import test from "node:test";
import "./helpers/viteCompat.ts";

const { dispatchAgentEvent } = await import("../src/api/stream.ts");
const { useChatStore, dtoToMessage } =
  await import("../src/store/chatStore.ts");

for (const error of [
  "The model repeatedly returned invalid tool arguments; these calls were not executed.",
  "Cannot prepare a safe context budget: The current request, execution state and latest tool feedback cannot fit even after compaction.",
]) {
  test(`terminal SSE error is visible immediately without replacing partial text: ${error}`, () => {
    useChatStore.setState({
      currentConversationId: "elsewhere",
      isStreaming: true,
      conversations: [
        {
          id: "qa",
          messages: [
            {
              id: "reply",
              role: "assistant",
              content: "Already generated.",
              completionStatus: "streaming",
              isStreaming: true,
              blocks: [
                { id: "text", type: "text", content: "Already generated." },
                {
                  id: "thought",
                  type: "thinking",
                  content: "Checking",
                  duration: 2,
                },
              ],
            },
          ],
        },
        {
          id: "elsewhere",
          messages: [{ id: "other", role: "assistant", content: "Unrelated" }],
        },
      ],
    } as never);
    let finished = false;
    const callbacks = {
      onToken() {},
      onThinkingStart() {},
      onThinkingToken() {},
      onThinkingDone() {},
      onGenerationDone() {},
      onDone() {
        finished = true;
      },
      onError(message: string) {
        useChatStore.getState().appendErrorBlock("qa", "reply", message);
        useChatStore.getState().setStreaming("qa", "reply", false);
      },
    };
    dispatchAgentEvent({ event: "error", error }, callbacks);
    const current = useChatStore.getState().conversations[0].messages[0];
    assert.equal(
      finished,
      false,
      "Error is displayed before transport completion",
    );
    assert.equal(current.content, "Already generated.");
    assert.equal(current.completionStatus, "error");
    assert.equal(current.isStreaming, false);
    assert.deepEqual(
      current.blocks?.map((block) => block.type),
      ["text", "thinking", "error"],
    );
    assert.equal(current.blocks?.at(-1)?.content, error);
    assert.equal(
      useChatStore.getState().conversations[1].messages[0].content,
      "Unrelated",
    );
    dispatchAgentEvent({ event: "error", error }, callbacks);
    assert.equal(
      useChatStore.getState().conversations[0].messages[0].blocks?.length,
      3,
    );
    const restored = dtoToMessage({
      id: "reply",
      role: "assistant",
      content: current.content,
      blocks: current.blocks,
      completion_status: "error",
    } as never);
    assert.equal(restored.blocks?.at(-1)?.type, "error");
    assert.equal(restored.blocks?.at(-1)?.content, error);
  });
}

test("terminal errors preserve legacy text and render even without prior blocks", () => {
  useChatStore.setState({
    conversations: [
      {
        id: "qa",
        messages: [
          { id: "legacy", role: "assistant", content: "Saved text" },
          { id: "blank", role: "assistant", content: "" },
        ],
      },
    ],
  } as never);
  useChatStore.getState().appendErrorBlock("qa", "legacy", "Failed");
  useChatStore.getState().appendErrorBlock("qa", "blank", "Failed");
  const messages = useChatStore.getState().conversations[0].messages;
  assert.deepEqual(
    messages[0].blocks?.map((b) => b.type),
    ["text", "error"],
  );
  assert.equal(messages[0].blocks?.[0].content, "Saved text");
  assert.deepEqual(
    messages[1].blocks?.map((b) => b.type),
    ["error"],
  );
});

for (const [type, name] of [
  ["quizzes", "create_quiz"],
  ["flashcards", "create_flashcard_deck"],
  ["notes", "create_study_note"],
]) {
  test(`${name}: terminal-only validation errors remain visible in chronological order and deduplicate on replay`, () => {
    useChatStore.setState({
      conversations: [
        {
          id: "qa",
          messages: [
            { id: "reply", role: "assistant", content: "", blocks: [] },
          ],
        },
      ],
    } as never);
    const callbacks = {
      onToken() {},
      onThinkingStart() {
        useChatStore.getState().startThinkingBlock("qa", "reply");
      },
      onThinkingToken(text: string) {
        useChatStore.getState().appendThinkingToken("qa", "reply", text);
      },
      onThinkingDone(duration: number) {
        useChatStore.getState().finishThinkingBlock("qa", "reply", duration);
      },
      onGenerationDone() {},
      onDone() {
        assert.fail("A tool error must not finish the agent turn");
      },
      onError() {
        assert.fail("A tool error is not a transport error");
      },
      onToolCallStart(call: never) {
        useChatStore.getState().startToolCallBlock("qa", "reply", call);
      },
      onToolCallUpdate(id: string, updates: never) {
        useChatStore.getState().updateToolCallBlock("qa", "reply", id, updates);
      },
    };
    dispatchAgentEvent({ event: "thinking_start" }, callbacks);
    dispatchAgentEvent({ event: "thinking", thinking: "Attempt" }, callbacks);
    dispatchAgentEvent(
      { event: "thinking_done", thinkingDuration: 2 },
      callbacks,
    );
    const rejected = {
      id: "rejected",
      type,
      status: "error",
      title: `Invalid arguments for ${name}`,
      error: "arguments.questions: expected array",
      startedAt: 1700000000000,
      completedAt: 1700000000000,
      durationMs: 0,
    };
    dispatchAgentEvent({ event: "tool_call", tool_call: rejected }, callbacks);
    dispatchAgentEvent({ event: "tool_call", tool_call: rejected }, callbacks);
    dispatchAgentEvent({ event: "thinking_start" }, callbacks);
    const blocks = useChatStore.getState().conversations[0].messages[0].blocks!;
    assert.deepEqual(
      blocks.map((b) => b.type),
      ["thinking", "tool_call", "thinking"],
    );
    assert.equal(blocks[1].toolCall?.error, rejected.error);
    assert.equal(blocks[0].content, "Attempt");
    assert.equal(blocks[1].toolCall?.status, "error");
    assert.equal(blocks[1].toolCall?.durationMs, 0);

    const reloaded = dtoToMessage({
      id: "reply",
      role: "assistant",
      content: "",
      blocks: [{ type: "tool_call", tool_call: rejected }],
    } as never);
    assert.equal(reloaded.blocks?.[0].toolCall?.error, rejected.error);
  });
}

test("a completed update does not duplicate an already started tool call", () => {
  const calls = new Map();
  const callbacks = {
    onToken() {},
    onThinkingStart() {},
    onThinkingToken() {},
    onThinkingDone() {},
    onGenerationDone() {},
    onDone() {},
    onError() {},
    onToolCallStart(call: { id: string }) {
      if (!calls.has(call.id)) calls.set(call.id, call);
    },
    onToolCallUpdate(id: string, updates: object) {
      Object.assign(calls.get(id), updates);
    },
  };
  dispatchAgentEvent(
    {
      event: "tool_call",
      tool_call: {
        id: "call",
        type: "quizzes",
        title: "Create quiz",
        status: "running",
        startedAt: 1700000000000,
      },
    },
    callbacks,
  );
  dispatchAgentEvent(
    {
      event: "tool_call",
      tool_call: {
        id: "call",
        type: "quizzes",
        status: "completed",
        completedAt: 1700000000100,
        genResults: [{ type: "quiz", quiz_id: "saved" }],
      },
    },
    callbacks,
  );
  assert.equal(calls.size, 1);
  assert.equal(calls.get("call").status, "completed");
});
