import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import "./helpers/viteCompat.ts";
import { toolAccessDetails } from "../src/lib/toolAccessDetails.ts";

const { dispatchAgentEvent } = await import("../src/api/stream.ts");
const { useChatStore, dtoToMessage } =
  await import("../src/store/chatStore.ts");

test("tool access distinguishes requested, loaded and already available schemas", () => {
  assert.deepEqual(
    toolAccessDetails(
      JSON.stringify({
        operation: "load_schema",
        requested: ["a", "b"],
        loaded: ["a"],
        already_loaded: ["b"],
      }),
    ),
    {
      deferred: false,
      readRequired: false,
      requiredTools: [],
      requested: ["a", "b"],
      loaded: ["a"],
      alreadyLoaded: ["b"],
    },
  );
  assert.equal(
    toolAccessDetails('{"operation":"deferred_call"}').deferred,
    true,
  );
  assert.deepEqual(toolAccessDetails('{"loaded":["a",2]}').requested, ["a"]);
  assert.deepEqual(
    toolAccessDetails(
      "Loaded create_study_note. The requested action has not executed.",
    ).loaded,
    ["create_study_note"],
  );
  for (const output of [undefined, "broken", "null", "[]"]) {
    assert.deepEqual(toolAccessDetails(output).requested, []);
  }
});

test("deferred warnings and schema details appear live, survive reload and deduplicate replay", () => {
  useChatStore.setState({
    conversations: [
      {
        id: "qa",
        messages: [
          {
            id: "reply",
            role: "assistant",
            content: "",
            isStreaming: true,
            blocks: [],
          },
        ],
      },
    ],
  } as never);
  const callbacks = {
    onToken() {},
    onThinkingStart() {},
    onThinkingToken() {},
    onThinkingDone() {},
    onGenerationDone() {},
    onDone() {
      assert.fail("A deferred call must not finish streaming");
    },
    onError() {
      assert.fail("A deferred call is not a transport failure");
    },
    onToolCallStart(call: never) {
      useChatStore.getState().startToolCallBlock("qa", "reply", call);
    },
    onToolCallUpdate(id: string, updates: never) {
      useChatStore.getState().updateToolCallBlock("qa", "reply", id, updates);
    },
  };
  const deferred = {
    id: "call",
    type: "tool_access",
    status: "warning",
    title: "create_study_note",
    startedAt: 1700000000000,
    completedAt: 1700000000000,
    durationMs: 0,
    output: JSON.stringify({
      operation: "deferred_call",
      requested: ["create_study_note"],
      action_executed: false,
    }),
  };
  const loaded = {
    ...deferred,
    id: "call-schema",
    status: "completed",
    output: JSON.stringify({
      operation: "load_schema",
      loaded: ["create_study_note"],
      action_executed: false,
    }),
  };
  for (const tool_call of [deferred, loaded, deferred, loaded]) {
    dispatchAgentEvent({ event: "tool_call", tool_call }, callbacks);
  }
  const message = useChatStore.getState().conversations[0].messages[0];
  assert.equal(message.isStreaming, true);
  assert.deepEqual(
    message.blocks?.map((block) => block.toolCall?.status),
    ["warning", "completed"],
  );
  assert.equal(message.blocks?.[0].toolCall?.title, "create_study_note");
  const restored = dtoToMessage({
    id: "reply",
    role: "assistant",
    content: "",
    blocks: [deferred, loaded].map((tool_call) => ({
      type: "tool_call",
      tool_call,
    })),
  } as never);
  assert.deepEqual(
    restored.blocks?.map((block) => block.toolCall?.output),
    [deferred.output, loaded.output],
  );
  assert.equal(restored.blocks?.[0].toolCall?.status, "warning");
});

test("access details default to collapsed and warning presentation is translated", () => {
  const message = readFileSync(
    new URL("../src/components/chat/MessageBubble.tsx", import.meta.url),
    "utf8",
  );
  assert.match(message, /tc\.type !== "tool_access" &&/);
  assert.match(message, /tc\.status === "warning"[\s\S]*text-warning/);
  assert.match(message, /<ToolAccessDetail tc=\{tc\}/);
  for (const locale of ["en", "fr", "ar"]) {
    const translations = JSON.parse(
      readFileSync(
        new URL(`../src/i18n/locales/${locale}.json`, import.meta.url),
        "utf8",
      ),
    );
    for (const key of [
      "deferred",
      "readRequired",
      "requiredStep",
      "readRequiredInfo",
      "alreadyDone",
      "requested",
      "loaded",
      "alreadyLoaded",
      "deferredInfo",
      "loadInfo",
      "loadingInfo",
    ]) {
      assert.ok(translations[`tool.access.${key}`]);
    }
  }
});

test("read prerequisites use concise details, including old persisted repair prompts", () => {
  const output = JSON.stringify({
    operation: "read_required",
    requested: ["update_artifact"],
    required_tools: ["read_artifact"],
    action_executed: false,
  });
  const legacy =
    "This update_artifact call was NOT executed. Its read prerequisite is unmet: No successful read exists.\n\nCall read_artifact for the target artifact first.";
  for (const value of [output, legacy]) {
    const details = toolAccessDetails(value);
    assert.equal(details.readRequired, true);
    assert.equal(details.deferred, false);
    assert.deepEqual(details.requiredTools, ["read_artifact"]);
    assert.deepEqual(details.requested, ["update_artifact"]);
    assert.deepEqual(details.loaded, []);
  }
  const restored = dtoToMessage({
    id: "reply",
    role: "assistant",
    content: "",
    blocks: [
      {
        type: "tool_call",
        tool_call: {
          id: "guard",
          type: "tool_access",
          status: "warning",
          output,
        },
      },
    ],
  } as never);
  assert.equal(
    toolAccessDetails(restored.blocks?.[0].toolCall?.output).readRequired,
    true,
  );
  const component = readFileSync(
    new URL("../src/components/chat/MessageBubble.tsx", import.meta.url),
    "utf8",
  );
  assert.match(
    component,
    /access\?\.readRequired[\s\S]*tool\.access\.readRequired/,
  );
  assert.match(
    component,
    /access\.readRequired[\s\S]*tool\.access\.readRequiredInfo/,
  );
  assert.match(
    component,
    /tc\.type === "artifact"[\s\S]*toolAccessDetails\(tc\.output\)\.readRequired/,
  );
});
