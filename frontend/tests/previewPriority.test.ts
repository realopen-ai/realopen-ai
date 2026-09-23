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
