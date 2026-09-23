import assert from "node:assert/strict";
import test from "node:test";
import { formatCodeExecOutput } from "../src/lib/codeExecOutput.ts";

test("command JSON displays stdout with its line breaks", () => {
  const output = JSON.stringify({
    command_id: "command",
    exit_code: 0,
    stdout: "first line\nsecond line\n",
    stderr: "",
  });

  assert.equal(formatCodeExecOutput(output), "first line\nsecond line\n");
});

test("command JSON displays stderr when stdout is empty", () => {
  const output = JSON.stringify({
    command_id: "command",
    exit_code: 1,
    stdout: "",
    stderr: "command failed\ntry again\n",
  });

  assert.equal(formatCodeExecOutput(output), "command failed\ntry again\n");
});

test("plain and unrelated JSON output remains unchanged", () => {
  assert.equal(formatCodeExecOutput("plain output\n"), "plain output\n");
  assert.equal(formatCodeExecOutput('{"value":42}'), '{"value":42}');
});
