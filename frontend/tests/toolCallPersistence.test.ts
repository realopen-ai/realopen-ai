import test from "node:test";
import assert from "node:assert/strict";

import { persistedToolCallDetails } from "../src/store/toolCallPersistence.ts";

test("coder file and preview details survive reload and live SSE mapping", () => {
  assert.deepEqual(
    persistedToolCallDetails({
      filePath: "/workspace/main.py",
      fileContent: "print('ok')",
      diff: "+print('ok')",
      previewUrl: "/api/sandboxes/id/preview/8000/",
      previewPort: 8000,
      unrelated: "discarded",
    }),
    {
      filePath: "/workspace/main.py",
      fileContent: "print('ok')",
      diff: "+print('ok')",
      previewUrl: "/api/sandboxes/id/preview/8000/",
      previewPort: 8000,
    },
  );
});
