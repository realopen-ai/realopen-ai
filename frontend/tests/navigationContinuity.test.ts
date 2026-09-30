import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appLayout = readFileSync(
  new URL("../src/components/layout/AppLayout.tsx", import.meta.url),
  "utf8",
);
const voiceSession = readFileSync(
  new URL("../src/voice/useVoiceSession.ts", import.meta.url),
  "utf8",
);

test("routed application pages keep the chat runtime mounted", () => {
  const desktopRuntime = appLayout.indexOf("<ChatArea />");
  const workspaceOverlay = appLayout.indexOf("showWorkspacePage &&");
  const brainOverlay = appLayout.indexOf("showBrainPage &&");

  assert.ok(desktopRuntime >= 0);
  assert.ok(workspaceOverlay > desktopRuntime);
  assert.ok(brainOverlay > desktopRuntime);
  assert.doesNotMatch(
    appLayout,
    /showWorkspacePage\s*\?\s*\(\s*<WorkspacePage/,
  );
});

test("voice sessions are ended explicitly, not by conversation navigation", () => {
  assert.doesNotMatch(
    voiceSession,
    /conversation changed — stopping voice session/,
  );
  assert.doesNotMatch(
    voiceSession,
    /client\.activeConversationId\s*!==\s*options\.conversationId/,
  );
  assert.match(voiceSession, /clientRef\.current\?\.stop\(\)/);
  assert.match(voiceSession, /clientRef\.current\?\.destroy\(\)/);
});
