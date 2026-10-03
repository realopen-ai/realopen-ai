import test from "node:test";
import assert from "node:assert/strict";

import {
  sandboxResourcePayload,
  sandboxResourcesAreValid,
} from "../src/store/sandboxResources.ts";

test("sandbox resources convert UI gigabytes to backend units", () => {
  assert.deepEqual(sandboxResourcePayload(2, 5), {
    memoryLimitMb: 2048,
    workspaceQuotaBytes: 5 * 1024 ** 3,
  });
});

test("sandbox resources enforce backend limits", () => {
  assert.equal(sandboxResourcesAreValid(2, 2, 2), true);
  assert.equal(sandboxResourcesAreValid(9, 2, 2), false);
  assert.equal(sandboxResourcesAreValid(2, 17, 2), false);
  assert.equal(sandboxResourcesAreValid(2, 2, 51), false);
});
