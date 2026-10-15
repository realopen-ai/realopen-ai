import assert from "node:assert/strict";
import test from "node:test";
import "./helpers/viteCompat.ts";
import { installFetch, jsonResponse } from "./helpers/fetchMock.ts";
import type { ToolCallResult } from "../src/store/chatStore.ts";
const { dispatchAgentEvent } = await import("../src/api/stream.ts");
const { useSandboxStore } = await import("../src/store/sandboxStore.ts");

test("scaffold progress and completion refresh the workspace through live SSE", async () => {
  useSandboxStore.setState({ sandboxId: "workspace", fileTree: [] });
  const pending: Promise<void>[] = [];
  const updates: Partial<ToolCallResult>[] = [];
  let starts = 0;
  const mock = installFetch((url) => {
    assert.equal(url, "/api/sandboxes/workspace/files");
    return jsonResponse({
      tree: [{ name: "app.py", path: "/workspace/app.py", type: "file" }],
    });
  });
  const callbacks = {
    onToken() {},
    onThinkingStart() {},
    onThinkingToken() {},
    onThinkingDone() {},
    onGenerationDone() {},
    onDone() {},
    onError() {},
    onToolCallStart() {
      starts++;
    },
    onToolCallUpdate(_id: string, update: Partial<ToolCallResult>) {
      updates.push(update);
      if (update.refreshFiles)
        pending.push(useSandboxStore.getState().fetchFileTree());
    },
  };
  try {
    dispatchAgentEvent(
      {
        event: "tool_call",
        tool_call: {
          id: "scaffold",
          sandboxId: "workspace",
          refreshFiles: true,
        },
      },
      callbacks,
    );
    await Promise.all(pending);
    assert.equal(
      starts,
      0,
      "file-tree invalidation must not create a duplicate tool card",
    );
    assert.equal(useSandboxStore.getState().fileTree[0].name, "app.py");
    for (const status of ["completed", "error"]) {
      dispatchAgentEvent(
        {
          event: "tool_call",
          tool_call: {
            id: "scaffold",
            type: "code_exec",
            status,
            refreshFiles: true,
          },
        },
        callbacks,
      );
    }
    await Promise.all(pending);
    assert.equal(mock.calls.length, 3);
    assert.deepEqual(
      updates.map((update) => update.refreshFiles),
      [true, true, true],
    );
  } finally {
    mock.restore();
  }
});

test("a stale empty-tree response cannot overwrite a newer scaffold refresh", async () => {
  useSandboxStore.setState({ sandboxId: "workspace", fileTree: [] });
  let finishOld!: (response: Response) => void;
  let calls = 0;
  const mock = installFetch(() =>
    ++calls === 1
      ? new Promise<Response>((resolve) => {
          finishOld = resolve;
        })
      : jsonResponse({
          tree: [{ name: "app.py", path: "/workspace/app.py", type: "file" }],
        }),
  );
  try {
    const old = useSandboxStore.getState().fetchFileTree();
    await useSandboxStore.getState().fetchFileTree();
    finishOld(jsonResponse({ tree: [] }));
    await old;
    assert.equal(useSandboxStore.getState().fileTree[0].name, "app.py");
    assert.equal(useSandboxStore.getState().isLoadingTree, false);
  } finally {
    mock.restore();
  }
});
