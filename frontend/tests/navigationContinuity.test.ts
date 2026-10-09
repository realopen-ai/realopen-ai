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
  const desktopRuntime = appLayout.indexOf("<ChatArea");
  const workspaceOverlay = appLayout.indexOf("showWorkspacePage &&");
  const brainOverlay = appLayout.indexOf("showBrainPage &&");

  assert.ok(desktopRuntime >= 0);
  assert.ok(workspaceOverlay > desktopRuntime);
  assert.ok(brainOverlay > desktopRuntime);
  assert.match(appLayout, /notebookConversationId=\{notebookConversationId\}/);
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

test("notebooks collapse ordinary workspace panes and use tabs below wide desktop widths", () => {
  assert.match(
    appLayout,
    /if \(showNotebook\) \{\s*if \(!panel.isCollapsed\(\)\) panel.collapse\(\);\s*return;/,
  );
  assert.match(appLayout, /\[rightPanelOpen, showNotebook\]/);
  assert.match(appLayout, /min-width: 1280px/);
  assert.match(appLayout, /xl:ms-64 xl:me-72/);
});

test("notebook chat deck artifacts open the notebook study experience", () => {
  const artifact = readFileSync(
    new URL("../src/components/learn/DeckArtifact.tsx", import.meta.url),
    "utf8",
  );
  const workspace = readFileSync(
    new URL("../src/components/learn/NotebookWorkspace.tsx", import.meta.url),
    "utf8",
  );
  assert.match(artifact, /new CustomEvent\("notebook-study"/);
  assert.match(
    workspace,
    /window.addEventListener\("notebook-study", listener\)/,
  );
  assert.match(workspace, /detail\?\.notebookId === id/);
  assert.match(workspace, /<StudySession deckId=\{study\}/);
});
