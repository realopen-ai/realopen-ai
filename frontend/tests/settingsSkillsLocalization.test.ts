import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/components/brain/SkillsTab.tsx", import.meta.url),
  "utf8",
);
const locales = ["en", "fr", "ar"].map((language) =>
  JSON.parse(
    readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), "utf8"),
  ),
);

test("voice, dependencies, notifications, and skills are localized", () => {
  const keys = [
    "settings.voice",
    "settings.dependencies",
    "settings.notifyDeepSearchDescription",
    "settings.voice.cloneWav",
    "settings.dependencies.install",
    "brain.skills.instructions",
    "brain.skills.availableTo",
    "brain.skills.resources",
    "brain.skills.closePreview",
  ];
  for (const locale of locales) {
    for (const key of keys) assert.equal(typeof locale[key], "string", key);
  }
});

test("skill markdown and resource previews remain left-to-right", () => {
  assert.match(source, /dir="ltr"/);
  assert.match(source, /text-left/);
});
