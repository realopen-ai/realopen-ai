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

test("tree edge cases: empty workspace, MB sizes, and invalid shapes pass through", () => {
  // Empty tree → the explicit empty marker.
  assert.equal(formatWorkspaceTreeOutput('{"tree": []}'), "/workspace (empty)");

  // MB-sized files format with one decimal.
  assert.equal(
    formatWorkspaceTreeOutput(
      JSON.stringify({
        tree: [{ name: "model.bin", type: "file", size: 3 * 1024 * 1024 }],
      }),
    ),
    ["/workspace", "└── model.bin (3.0 MB)"].join("\n"),
  );

  // A `tree` that is not an array is not a list-files envelope.
  assert.equal(
    formatWorkspaceTreeOutput('{"tree": "not-a-list"}'),
    '{"tree": "not-a-list"}',
  );

  // Nodes missing name/type are rejected → verbatim passthrough.
  assert.equal(
    formatWorkspaceTreeOutput('{"tree": [{"path": "/x", "type": "file"}]}'),
    '{"tree": [{"path": "/x", "type": "file"}]}',
  );

  // Malformed JSON is never thrown on.
  assert.equal(formatWorkspaceTreeOutput("not json {"), "not json {");
});
