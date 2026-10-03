import assert from "node:assert/strict";
import test from "node:test";

import {
  completionAction,
  isViewingConversation,
} from "../src/lib/completionNotificationPolicy.ts";
import { readFileSync } from "node:fs";

test("does not notify for the visible conversation", () => {
  assert.equal(
    completionAction({
      origin: "text",
      appActive: true,
      viewingConversation: true,
    }),
    "none",
  );
});

test("uses an in-app toast when active elsewhere", () => {
  assert.equal(
    completionAction({
      origin: "text",
      appActive: true,
      viewingConversation: false,
    }),
    "toast",
  );
});

test("uses a browser notification when hidden or unfocused", () => {
  assert.equal(
    completionAction({
      origin: "text",
      appActive: false,
      viewingConversation: false,
    }),
    "browser",
  );
});

test("voice responses never produce completion notifications", () => {
  for (const appActive of [true, false]) {
    assert.equal(
      completionAction({
        origin: "voice",
        appActive,
        viewingConversation: false,
      }),
      "none",
    );
  }
});

test("conversation route matching is exact and URL-safe", () => {
  assert.equal(isViewingConversation("/abc-123", "abc-123"), true);
  assert.equal(isViewingConversation("/", "abc-123", "abc-123"), true);
  assert.equal(isViewingConversation("/brain", "abc-123"), false);
  assert.equal(isViewingConversation("/other", "abc-123"), false);
});

test("completion events include an assistant response preview", () => {
  const source = readFileSync(
    new URL("../src/components/chat/ChatArea.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /responsePreview:\s*completionPreview\(current\?\.content/);
});

test("the sound context is unlocked by normal user interaction", () => {
  const source = readFileSync(
    new URL(
      "../src/assets/sounds/response-complete.ts",
      import.meta.url,
    ),
    "utf8",
  );
  assert.match(source, /addEventListener\("pointerdown", unlock/);
  assert.match(source, /audioContext \?\?=/);
  assert.doesNotMatch(source, /current\.close\(/);
});
