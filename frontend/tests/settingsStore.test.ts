/**
 * Tests for src/store/settingsStore.ts — persisted appearance/notifications
 * settings + the t()/useT() translation helpers.
 *
 * Two Node/Vite gaps are bridged for this module (see tests/helpers/viteCompat.ts):
 *   - it imports `@/i18n`, which Node cannot resolve (alias hook), and which
 *     itself imports ./locales/*.json — Vite serves those without Node's
 *     required import attributes (json load hook);
 *   - zustand/persist needs window.localStorage, so a fake is installed
 *     before the dynamic import (same as tests/uiStore.test.ts).
 */
import assert from "node:assert/strict";
import test from "node:test";

import { createElement } from "react";
import { renderToString } from "react-dom/server";

import "./helpers/viteCompat.ts";

// ── Fake localStorage (must exist before the store evaluates) ─────────
const storage = new Map<string, string>();
const g = globalThis as typeof globalThis & { window?: unknown };
g.window = {
  localStorage: {
    getItem: (key: string) => (storage.has(key) ? storage.get(key)! : null),
    setItem: (key: string, value: string) => void storage.set(key, value),
    removeItem: (key: string) => void storage.delete(key),
  },
};

const { accentColorMap, fontSizeOptions, t, useT, useSettingsStore } =
  await import("../src/store/settingsStore.ts");

test("fresh store starts from the documented defaults", () => {
  const s = useSettingsStore.getState();
  assert.equal(s.appearance, "system");
  assert.equal(s.contrast, "medium");
  assert.equal(s.accentColor, "gray");
  assert.equal(s.language, "en");
  assert.equal(s.fontSize, "14px");
  assert.equal(s.notifyDeepSearch, true);
  assert.equal(s.completionSound, true);
  assert.equal(s.browserCompletionNotifications, true);
  assert.equal(s.inAppCompletionNotifications, true);
});

test("every setting has a setter and is persisted under realopen-ai-settings", () => {
  const s = useSettingsStore.getState();
  s.setAppearance("dark");
  s.setContrast("increased");
  s.setAccentColor("purple");
  s.setLanguage("fr");
  s.setFontSize("18px");
  s.setNotifyDeepSearch(false);
  s.setCompletionSound(false);
  s.setBrowserCompletionNotifications(false);
  s.setInAppCompletionNotifications(false);

  const updated = useSettingsStore.getState();
  assert.equal(updated.appearance, "dark");
  assert.equal(updated.contrast, "increased");
  assert.equal(updated.accentColor, "purple");
  assert.equal(updated.language, "fr");
  assert.equal(updated.fontSize, "18px");
  assert.equal(updated.notifyDeepSearch, false);
  assert.equal(updated.completionSound, false);
  assert.equal(updated.browserCompletionNotifications, false);
  assert.equal(updated.inAppCompletionNotifications, false);

  const persisted = JSON.parse(storage.get("realopen-ai-settings")!) as {
    state: { language: string; accentColor: string };
  };
  assert.equal(persisted.state.language, "fr");
  assert.equal(persisted.state.accentColor, "purple");
});

test("t() translates through the active language and falls back safely", () => {
  useSettingsStore.getState().setLanguage("en");
  assert.equal(t("sidebar.newChat"), "New chat");
  // Unknown keys fall through to the key itself (documented behavior).
  assert.equal(t("totally.missing.key"), "totally.missing.key");

  useSettingsStore.getState().setLanguage("fr");
  const frTranslation = t("sidebar.newChat");
  assert.notEqual(frTranslation, "sidebar.newChat");
  assert.notEqual(frTranslation, "New chat");
});

test("t() interpolates {placeholders} and keeps unmatched ones verbatim", () => {
  useSettingsStore.getState().setLanguage("en");
  assert.equal(
    t("voice.error.runtimeMissing", { name: "ffmpeg" }),
    "A required voice runtime package is missing: ffmpeg",
  );
  assert.equal(
    t("settings.ai.providers.modelsAvailable", { count: 7 }),
    "7 models available",
  );
  // A placeholder without a param is left as-is instead of being dropped.
  assert.equal(
    t("voice.error.default", { message: "boom" }),
    "Voice error: boom",
  );
  assert.equal(
    t("voice.error.default").includes("{message}"),
    true,
    "missing params must keep their placeholder",
  );
});

test("useT() returns a working translator when rendered through React", () => {
  // useT is a React hook, so it needs a render context. react-dom (already
  // a dependency) provides one server-side. Note: react-dom/server renders
  // with the store's SERVER snapshot, which zustand defines as
  // getInitialState() — so the translator below sees the initial language
  // ("en"), not the live one. The lookup + fallback chain is what is under
  // test; client-side renders read the live state via getSnapshot.
  useSettingsStore.getState().setLanguage("fr");
  let captured: string[] | undefined;
  function Probe() {
    const translate = useT();
    captured = [translate("sidebar.newChat"), translate("totally.missing.key")];
    return null;
  }
  renderToString(createElement(Probe));

  assert.deepEqual(captured, ["New chat", "totally.missing.key"]);
});

test("every accent color maps to a complete CSS value set", () => {
  assert.deepEqual(Object.keys(accentColorMap).sort(), [
    "black",
    "blue",
    "gray",
    "green",
    "orange",
    "pink",
    "purple",
    "yellow",
  ]);
  for (const values of Object.values(accentColorMap)) {
    assert.equal(typeof values.primary, "string");
    assert.equal(typeof values.primaryForeground, "string");
    assert.equal(typeof values.primaryHover, "string");
    assert.equal(typeof values.ring, "string");
    assert.equal(typeof values.glow, "string");
  }
});

test("font size options match the type-level choices", () => {
  assert.deepEqual(fontSizeOptions, ["14px", "16px", "18px"]);
});

test("a reload rehydrates persisted settings", async () => {
  storage.set(
    "realopen-ai-settings",
    JSON.stringify({
      state: { language: "fr", accentColor: "blue", fontSize: "16px" },
      version: 0,
    }),
  );
  await useSettingsStore.persist.rehydrate();

  const s = useSettingsStore.getState();
  assert.equal(s.language, "fr");
  assert.equal(s.accentColor, "blue");
  assert.equal(s.fontSize, "16px");
  // t() reads the live store, so the rehydrated language applies immediately.
  assert.equal(t("sidebar.newChat"), "Nouvelle conversation");
});
