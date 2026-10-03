import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const bubble = readFileSync(
  new URL("../src/components/chat/MessageBubble.tsx", import.meta.url),
  "utf8",
);

const locales = ["en", "fr", "ar"].map((language) =>
  JSON.parse(
    readFileSync(
      new URL(`../src/i18n/locales/${language}.json`, import.meta.url),
      "utf8",
    ),
  ),
);

test("thinking duration is rounded and localized", () => {
  assert.match(bubble, /Math\.round\(block\.duration \?\? 0\)/);
  assert.match(bubble, /translate\("message\.thoughtFor"/);
  assert.doesNotMatch(bubble, /`Thought for/);
  for (const locale of locales) {
    assert.equal(typeof locale["message.thoughtFor"], "string");
    assert.match(locale["message.thoughtFor"], /\{seconds\}/);
  }
});

test("tool call labels and details exist in every locale", () => {
  const requiredKeys = [
    "tool.code.done",
    "tool.code.running",
    "tool.fileRead.done",
    "tool.fileWrite.done",
    "tool.detail.writtenContent",
    "tool.detail.result",
    "tool.detail.output",
    "tool.detail.exitCode",
    "tool.detail.openPreview",
  ];
  for (const locale of locales) {
    for (const key of requiredKeys) {
      assert.equal(typeof locale[key], "string", `missing ${key}`);
    }
  }
});
