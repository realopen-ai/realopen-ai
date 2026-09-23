import test from "node:test";
import assert from "node:assert/strict";

import { shouldLoadConversationWorkspace } from "../src/lib/workspaceActivation.ts";

test("pending home-page conversations do not clear a streamed workspace", () => {
  assert.equal(shouldLoadConversationWorkspace(null), false);
  assert.equal(shouldLoadConversationWorkspace("conversation-id"), true);
});
