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

test("CRUD scaffolds show generated resources and stack in chat and terminal", () => {
  const output = JSON.stringify({
    created_files: ["/workspace/backend/app.py"],
    project: {
      backend: { assets: "fastapi" },
      frontend: { assets: "vite", framework: "react", styling: "tailwind" },
    },
    scaffold: {
      generator: "crud",
      features: { crud: 1 },
      resources: ["products", "contacts"],
    },
  });
  const options = { toolName: "scaffold_crud_app" };
  assert.equal(
    presentCoderToolOutput(output, options)?.summary,
    "Created 1 file",
  );
  const terminal = formatCoderToolOutput(output, options);
  assert.match(terminal, /fastapi/);
  assert.match(terminal, /vite/);
  assert.match(terminal, /tailwind/);
  assert.match(terminal, /react/);
  assert.match(terminal, /products, contacts/);
  assert.doesNotMatch(terminal, /\{"/);
});
