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

test("saved plans are readable in chat and terminal without becoming verification", () => {
  const output = JSON.stringify({
    saved_plan: {
      steps: [
        { task: "Scaffold the project", status: "completed" },
        { task: "Implement filtering", status: "in_progress" },
        { task: "Run checks", status: "pending" },
      ],
      next_action: "Edit the filter handler",
      notes: "Keep the existing API.\nUse isolated test data.",
      files: ["/workspace/ui.tsx"],
    },
  });
  for (const options of [{ toolName: "update_plan" }, {}]) {
    const view = presentCoderToolOutput(output, options);
    assert.equal(view?.plan?.steps.length, 3);
    assert.equal(view?.plan?.nextAction, "Edit the filter handler");
    assert.deepEqual(view?.checks, []);
    assert.deepEqual(view?.files, []);
    const terminal = formatCoderToolOutput(output, options);
    assert.match(terminal, /\[Completed\] Scaffold the project/);
    assert.match(terminal, /\[In progress\] Implement filtering/);
    assert.match(terminal, /\[Pending\] Run checks/);
    assert.match(terminal, /not execution verification/);
    assert.match(terminal, /Next action:\n  Edit the filter handler/);
    assert.match(terminal, /Working files:\n  \/workspace\/ui.tsx/);
    assert.doesNotMatch(terminal, /saved_plan|"steps"/);
  }
  assert.equal(JSON.parse(output).saved_plan.steps[0].status, "completed");
});

test("malformed plans fall back safely and optional plan details may be absent", () => {
  const minimal = {
    saved_plan: {
      steps: [{ task: "Inspect source", status: "pending" }],
      next_action: "Read the target file",
    },
  };
  assert.equal(
    presentCoderToolOutput(JSON.stringify(minimal))?.plan?.steps.length,
    1,
  );
  for (const steps of [[], [null], [{ task: "Work", status: "invented" }]]) {
    assert.equal(
      presentCoderToolOutput(
        JSON.stringify({ saved_plan: { ...minimal.saved_plan, steps } }),
      ),
      null,
    );
  }
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
