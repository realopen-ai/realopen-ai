import assert from "node:assert/strict";
import test from "node:test";

import {
  formatCoderToolOutput,
  presentCoderToolOutput,
} from "../src/lib/coderToolPresentation.ts";

test("command envelopes retain readable stdout, stderr and exit status", () => {
  const result = presentCoderToolOutput(
    JSON.stringify({
      command_id: "check-1",
      exit_code: 1,
      stdout: "2 tests passed\n",
      stderr: "1 test failed\n",
    }),
    { toolName: "run_tests" },
  );

  assert.equal(result?.summary, "Command failed");
  assert.equal(result?.stdout, "2 tests passed\n");
  assert.equal(result?.stderr, "1 test failed\n");
  assert.deepEqual(result?.facts, [
    { label: "Command ID", value: "check-1" },
    { label: "Exit code", value: "1" },
  ]);
});

test("unrelated JSON is not claimed by the coder presenter", () => {
  assert.equal(presentCoderToolOutput('{"answer":42}'), null);
  assert.equal(formatCoderToolOutput('{"answer":42}'), '{"answer":42}');
});
