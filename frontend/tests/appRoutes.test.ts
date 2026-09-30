import assert from "node:assert/strict";
import test from "node:test";

import {
  brainTabPath,
  getBrainRoute,
  getWorkspaceSection,
  isBrainRoute,
  isWorkspaceRoute,
  workspaceSectionPath,
} from "../src/lib/appRoutes.ts";

test("recognizes Brain routes and restores nested tab state", () => {
  assert.equal(isBrainRoute("/brain"), true);
  assert.equal(isBrainRoute("/brain/history"), true);
  assert.equal(isBrainRoute("/workspace"), false);
  assert.deepEqual(getBrainRoute("/brain"), {
    tab: "memories",
    toolId: null,
  });
  assert.deepEqual(getBrainRoute("/brain/history"), {
    tab: "history",
    toolId: null,
  });
  assert.deepEqual(getBrainRoute("/brain/tools/use_websearch"), {
    tab: "tools",
    toolId: "use_websearch",
  });
});

test("builds canonical Brain tab URLs", () => {
  assert.equal(brainTabPath("memories"), "/brain");
  assert.equal(brainTabPath("skills"), "/brain/skills");
});

test("recognizes and restores Workspace section routes", () => {
  assert.equal(isWorkspaceRoute("/workspace"), true);
  assert.equal(isWorkspaceRoute("/workspace/sandboxes"), true);
  assert.equal(isWorkspaceRoute("/brain"), false);
  assert.equal(getWorkspaceSection("/workspace"), null);
  assert.equal(
    getWorkspaceSection("/workspace/sandboxes"),
    "sandboxes",
  );
  assert.equal(workspaceSectionPath("documents"), "/workspace/documents");
});
