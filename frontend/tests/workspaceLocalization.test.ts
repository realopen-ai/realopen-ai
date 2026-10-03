import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const locales = ["en", "fr", "ar"].map((language) =>
  JSON.parse(
    readFileSync(
      new URL(`../src/i18n/locales/${language}.json`, import.meta.url),
      "utf8",
    ),
  ),
);

test("workspace pages have document, template, generated, and asset locales", () => {
  const keys = [
    "workspace.documents.search",
    "workspace.documents.uploadDocument",
    "workspace.documents.aiKnowledge",
    "workspace.templates.add",
    "workspace.templates.test",
    "workspace.generated.empty",
    "workspace.assets.emptyDescription",
  ];
  for (const locale of locales) {
    for (const key of keys) assert.equal(typeof locale[key], "string", key);
  }
});
