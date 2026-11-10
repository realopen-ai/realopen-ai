import assert from "node:assert/strict";
import test from "node:test";
import {
  activeCoderTask,
  coderTaskDuration,
  coderContextPercent,
} from "../src/lib/coderTaskProgress.ts";
import { controlCoderTask } from "../src/api/coderTasksClient.ts";
test("elapsed and estimated input budget handle long runs and missing readings", () => {
  assert.equal(coderTaskDuration(6012), "1h 40m");
  assert.equal(coderTaskDuration(65), "1m 5s");
  assert.equal(coderTaskDuration(-4), "0m 0s");
  assert.equal(
    coderContextPercent({ input_tokens: 1500, input_limit: 2000 }),
    75,
  );
  assert.equal(
    coderContextPercent({ input_tokens: 2500, input_limit: 2000 }),
    100,
  );
  assert.equal(coderContextPercent({}), null);
});
test("terminal runs are not active", () => {
  for (const status of [
    "paused",
    "interrupted",
    "failed",
    "cancelled",
    "completed",
  ])
    assert.equal(activeCoderTask({ status }), false);
  assert.equal(activeCoderTask({ status: "running" }), true);
});
test("controls scope requests and surface admission failures", async () => {
  const previous = globalThis.fetch;
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(
        url,
        "/api/coder/tasks/task/stop-after-round?conversation_id=conv",
      );
      assert.equal(init?.method, "POST");
      return Response.json(
        { detail: "Task is no longer running" },
        { status: 409 },
      );
    };
    await assert.rejects(
      controlCoderTask("conv", "task", "stop-after-round"),
      /no longer running/,
    );
  } finally {
    globalThis.fetch = previous;
  }
});
