import test from "node:test";
import assert from "node:assert/strict";

import { useSandboxStore } from "../src/store/sandboxStore.ts";

test("workspace lifecycle disables duplicate actions until completion", async () => {
  const originalFetch = globalThis.fetch;
  let finish!: () => void;
  let calls = 0;
  const pending = new Promise<void>((resolve) => {
    finish = resolve;
  });
  globalThis.fetch = (async () => {
    calls += 1;
    await pending;
    return new Response(
      JSON.stringify({
        id: "sandbox-1",
        name: "Test workspace",
        status: "running",
        desired_running: true,
        cpu_limit: 2,
        memory_limit_mb: 2048,
        workspace_quota_bytes: 1024,
        usage_bytes: 0,
        idle_timeout_seconds: 1800,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }) as typeof fetch;

  useSandboxStore.setState({ sandboxId: "sandbox-1", lifecyclePending: {} });
  const first = useSandboxStore.getState().lifecycle("start");
  const duplicate = useSandboxStore.getState().lifecycle("restart");

  assert.equal(
    useSandboxStore.getState().lifecyclePending["sandbox-1"],
    "start",
  );
  assert.equal(calls, 1);
  await duplicate;
  finish();
  await first;
  assert.equal(
    useSandboxStore.getState().lifecyclePending["sandbox-1"],
    undefined,
  );
  assert.equal(calls, 1);

  globalThis.fetch = originalFetch;
});
