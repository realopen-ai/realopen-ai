import test from "node:test";
import assert from "node:assert/strict";

import { useSandboxStore } from "../src/store/sandboxStore.ts";

test("frontend preview remains selected over backend preview", () => {
  useSandboxStore.setState({ previewUrl: null, previewPort: null });
  useSandboxStore.getState().selectPreview("/backend", 6969);
  assert.equal(useSandboxStore.getState().previewUrl, "/backend");

  useSandboxStore.getState().selectPreview("/frontend", 6767);
  useSandboxStore.getState().selectPreview("/late-backend", 6969);

  assert.equal(useSandboxStore.getState().previewUrl, "/frontend");
  assert.equal(useSandboxStore.getState().previewPort, 6767);
});

test("preview discovery restores a running preview after reload", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(
      JSON.stringify({
        available: [{ port: 6969, url: "/api/sandboxes/id/preview/6969/" }],
        preferred: {
          port: 6969,
          url: "/api/sandboxes/id/preview/6969/",
          host_url: "http://localhost:6969",
        },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )) as typeof fetch;
  try {
    useSandboxStore.setState({
      sandboxId: "id",
      previewUrl: null,
      previewPort: null,
    });
    await useSandboxStore.getState().discoverPreview("id");
    assert.equal(
      useSandboxStore.getState().previewUrl,
      "/api/sandboxes/id/preview/6969/",
    );
    assert.equal(useSandboxStore.getState().previewPort, 6969);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("linking a sandbox resets scoped state and discovers its preview", async () => {
  const originalFetch = globalThis.fetch;
  const urls: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    urls.push(url);
    const body = url.endsWith("/files")
      ? { tree: [] }
      : url.endsWith("/commands")
        ? { commands: [] }
        : url.endsWith("/previews")
          ? {
              preferred: {
                port: 6969,
                url: "/api/sandboxes/new/preview/6969/",
                host_url: "http://localhost:6969",
              },
            }
          : {};
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  try {
    useSandboxStore.setState({
      sandboxId: "old",
      fileTree: [{ name: "old.py", path: "/workspace/old.py", type: "file" }],
      activeFile: "/workspace/old.py",
      activeFileContent: "old",
      terminalHistory: ["old command"],
      previewUrl: "/old-preview",
      previewPort: 6767,
    });

    await useSandboxStore.getState().linkSandbox("new", "conversation");

    const state = useSandboxStore.getState();
    assert.equal(state.sandboxId, "new");
    assert.deepEqual(state.fileTree, []);
    assert.equal(state.activeFile, null);
    assert.equal(state.activeFileContent, null);
    assert.deepEqual(state.terminalHistory, []);
    assert.equal(state.previewUrl, "/api/sandboxes/new/preview/6969/");
    assert.equal(state.previewPort, 6969);
    assert.ok(urls.some((url) => url.endsWith("/previews")));
  } finally {
    globalThis.fetch = originalFetch;
  }
});
