import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const chatArea = readFileSync(
  new URL("../src/components/chat/ChatArea.tsx", import.meta.url),
  "utf8",
);

test("a completed model round does not finalize the full agent turn", () => {
  const generationHandler = chatArea.match(
    /onGenerationDone:[\s\S]*?(?=onMemoryExtractionStart:)/,
  )?.[0];

  assert.ok(generationHandler);
  assert.doesNotMatch(generationHandler, /setStreaming\(/);
  assert.match(chatArea, /onDone:[\s\S]*?setStreaming\([^)]*false\)/);
});
