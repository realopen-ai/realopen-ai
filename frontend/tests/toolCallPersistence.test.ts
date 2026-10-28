import test from "node:test";
import assert from "node:assert/strict";

import { persistedToolCallDetails } from "../src/store/toolCallPersistence.ts";

test("coder file and preview details survive reload and live SSE mapping", () => {
  assert.deepEqual(
    persistedToolCallDetails({
      agent: "coder",
      toolName: "write_file",
      parentId: "parent-call",
      filePath: "/workspace/main.py",
      fileContent: "print('ok')",
      skillName: "full-stack-app-builder",
      skillResource: "references/vite.md",
      diff: "+print('ok')",
      previewUrl: "/api/sandboxes/id/preview/8000/",
      previewPort: 8000,
      sandboxId: "workspace-id",
      sandbox: {
        id: "workspace-id",
        name: "Project workspace",
        status: "running",
      },
      unrelated: "discarded",
    }),
    {
      agent: "coder",
      toolName: "write_file",
      parentId: "parent-call",
      filePath: "/workspace/main.py",
      fileContent: "print('ok')",
      skillName: "full-stack-app-builder",
      skillResource: "references/vite.md",
      diff: "+print('ok')",
      previewUrl: "/api/sandboxes/id/preview/8000/",
      previewPort: 8000,
      sandboxId: "workspace-id",
      sandbox: {
        id: "workspace-id",
        name: "Project workspace",
        status: "running",
      },
    },
  );
});
