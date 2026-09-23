import test from "node:test";
import assert from "node:assert/strict";

import { visibleWorkspaceTree } from "../src/store/sandboxStore.ts";

test("sandbox tree hides generated dependency and Python cache directories", () => {
  const tree = visibleWorkspaceTree([
    { name: "main.py", path: "/workspace/main.py", type: "file" },
    { name: ".venv", path: "/workspace/.venv", type: "directory" },
    {
      name: "tests",
      path: "/workspace/tests",
      type: "directory",
      children: [
        { name: "__pycache__", path: "/workspace/tests/__pycache__", type: "directory" },
        { name: "test_app.py", path: "/workspace/tests/test_app.py", type: "file" },
      ],
    },
  ]);
  assert.deepEqual(tree.map((node) => node.name), ["main.py", "tests"]);
  assert.deepEqual(tree[1].children?.map((node) => node.name), ["test_app.py"]);
});
