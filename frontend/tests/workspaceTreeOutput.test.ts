import test from "node:test";
import assert from "node:assert/strict";

import { formatWorkspaceTreeOutput } from "../src/lib/workspaceTreeOutput.ts";

test("list-files JSON renders as a readable workspace tree", () => {
  const raw = JSON.stringify({
    tree: [
      {
        name: "fastapi-notes-app",
        path: "/workspace/fastapi-notes-app",
        type: "directory",
        children: [
          {
            name: "main.py",
            path: "/workspace/fastapi-notes-app/main.py",
            type: "file",
            size: 1645,
          },
          {
            name: "notes.db",
            path: "/workspace/fastapi-notes-app/notes.db",
            type: "file",
            size: 12288,
          },
        ],
      },
      {
        name: "pyproject.toml",
        path: "/workspace/pyproject.toml",
        type: "file",
        size: 202,
      },
    ],
  });

  assert.equal(
    formatWorkspaceTreeOutput(raw),
    [
      "/workspace",
      "├── fastapi-notes-app/",
      "│   ├── main.py (1.6 KB)",
      "│   └── notes.db (12.0 KB)",
      "└── pyproject.toml (202 B)",
    ].join("\n"),
  );
});

test("ordinary file JSON remains unchanged", () => {
  const raw = '{"status":"ok"}';
  assert.equal(formatWorkspaceTreeOutput(raw), raw);
});
