import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path: string) =>
  readFileSync(new URL(path, import.meta.url), "utf8");

test("Arabic is selectable and switches the document to RTL", () => {
  const locale = JSON.parse(read("../src/i18n/locales/ar.json"));
  const i18n = read("../src/i18n/index.ts");
  const settings = read("../src/components/settings/SettingsModal.tsx");
  const theme = read("../src/components/settings/ThemeManager.tsx");

  assert.equal(locale["settings.language.ar"], "العربية");
  const english = JSON.parse(read("../src/i18n/locales/en.json"));
  assert.deepEqual(Object.keys(locale).sort(), Object.keys(english).sort());
  assert.match(i18n, /translations = \{ en, fr, ar \}/);
  assert.match(settings, /value: "ar"/);
  assert.match(theme, /language === "ar" \? "rtl" : "ltr"/);
});

test("technical surfaces remain left-to-right in Arabic mode", () => {
  const css = read("../src/index.css");
  assert.match(css, /\.xterm[\s\S]*direction: ltr/);
});
