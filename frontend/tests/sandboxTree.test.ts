import test from "node:test";
import assert from "node:assert/strict";

import {
  commandHistoryLines,
  mergeSandboxList,
  visibleWorkspaceTree,
  type Sandbox,
} from "../src/store/sandboxStore.ts";

const sandbox = (id: string): Sandbox => ({
  id,
  name: `${id} workspace`,
  status: "running",
  desired_running: true,
  cpu_limit: 2,
  memory_limit_mb: 2048,
  workspace_quota_bytes: 1024,
  usage_bytes: 0,
  idle_timeout_seconds: 1800,
});

test("stale sandbox list responses preserve the streamed active workspace", () => {
  assert.deepEqual(
    mergeSandboxList([sandbox("older")], "new", [sandbox("new")]).map(
      (item) => item.id,
    ),
    ["new", "older"],
  );
  assert.deepEqual(
    mergeSandboxList([sandbox("new")], "new", [sandbox("new")]).map(
      (item) => item.id,
    ),
    ["new"],
  );
});

test("sandbox tree hides generated dependency and Python cache directories", () => {
  const tree = visibleWorkspaceTree([
    { name: "main.py", path: "/workspace/main.py", type: "file" },
    { name: ".venv", path: "/workspace/.venv", type: "directory" },
    {
      name: "tests",
      path: "/workspace/tests",
      type: "directory",
      children: [
        {
          name: "__pycache__",
          path: "/workspace/tests/__pycache__",
          type: "directory",
        },
        {
          name: "test_app.py",
          path: "/workspace/tests/test_app.py",
          type: "file",
        },
      ],
    },
  ]);
  assert.deepEqual(
    tree.map((node) => node.name),
    ["main.py", "tests"],
  );
  assert.deepEqual(
    tree[1].children?.map((node) => node.name),
    ["test_app.py"],
  );
});

test("sandbox command history renders only command streams", () => {
  const lines = commandHistoryLines([
    {
      id: "1",
      source: "coder_agent",
      tool_name: "run_command",
      command: "ls -la",
      stdout: "file.txt\n",
      stderr: "",
      exit_code: 0,
      output_truncated: false,
    },
    {
      id: "2",
      source: "general_agent",
      tool_name: "use_code_exec",
      command: "print(2 + 2)",
      stdout: "4\n",
      stderr: "",
      exit_code: 0,
      output_truncated: false,
    },
  ]);
  assert.deepEqual(lines, [
    "$ ls -la",
    "file.txt\n",
    "$ python\n  print(2 + 2)",
    "4\n",
  ]);
});

test("sandbox command history marks stderr and truncation", () => {
  const lines = commandHistoryLines([
    {
      id: "1",
      source: "coder_agent",
      tool_name: "run_tests",
      command: "pytest",
      stdout: "",
      stderr: "failed",
      exit_code: 1,
      output_truncated: true,
    },
  ]);
  assert.deepEqual(lines, [
    "$ pytest",
    "Error (exit 1):\nfailed",
    "  [output truncated]",
  ]);
});
