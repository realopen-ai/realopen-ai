import assert from "node:assert/strict";
import test from "node:test";
import {
  formatCodeExecOutput,
  generalCodeExecCommand,
} from "../src/lib/codeExecOutput.ts";

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

test("general code execution uses a compact synthetic Python command", () => {
  assert.equal(
    generalCodeExecCommand("tc:code/123"),
    "$ python3 /tmp/tc-code-123.py",
  );
});

test("coder scaffold results are presented as files and checks, not JSON", () => {
  const output = JSON.stringify({
    created_files: ["/workspace/backend/app.py", "/workspace/frontend/main.ts"],
    project: { backend: "fastapi", frontend: "vite" },
    setup: [
      {
        action: "python_setup",
        command: "uv sync --all-groups",
        exit_code: 0,
        result: JSON.stringify({
          stdout: "Installed 12 packages\n",
          stderr: "",
        }),
      },
    ],
  });

  const rendered = formatCodeExecOutput(output, {
    coder: true,
    toolName: "scaffold_project",
  });
  assert.match(rendered, /^✓ Created 2 files/m);
  assert.match(rendered, /\+ \/workspace\/backend\/app\.py/);
  assert.match(rendered, /✓ uv sync --all-groups/);
  assert.match(rendered, /Installed 12 packages/);
  assert.doesNotMatch(rendered, /"created_files"/);
});

test("coder verification reports display named pass and failure statuses", () => {
  const output = JSON.stringify({
    files_changed: ["/workspace/src/main.ts"],
    verification: {
      revision: 4,
      checks: {
        frontend_tests: { command: "bun test", status: "passed" },
        build: { command: "bun run build", status: "failed" },
      },
    },
  });

  const rendered = formatCodeExecOutput(output, { coder: true });
  assert.match(rendered, /✓ bun test/);
  assert.match(rendered, /✗ bun run build/);
  assert.doesNotMatch(rendered, /"verification"/);
});
