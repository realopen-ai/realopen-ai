/**
 * Tests for src/lib/appVisibility.ts — the "is the tab in the foreground"
 * tracker used by completion notifications/sounds.
 *
 * The module snapshots document.hasFocus()/visibilityState ONCE at import
 * and then listens for window focus/blur + document visibilitychange.
 * Two execution environments are exercised in this file:
 *
 *   1. Plain Node (no window/document): defaults to "active" — the static
 *      import at the top evaluates before any globals are installed.
 *   2. A browser-like environment: window/document fakes are installed
 *      BEFORE a dynamic import, so the module registers its listeners on
 *      our fakes and the tests can dispatch focus/blur/visibilitychange.
 *      The dynamic import carries a query string (?env=browser) so Node
 *      treats it as a separate module and re-evaluates it fresh.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { isAppActive as isAppActiveHeadless } from "../src/lib/appVisibility.ts";

type Listener = () => void;

const windowListeners = new Map<string, Listener>();
const documentListeners = new Map<string, Listener>();
const browser = {
  focused: false,
  visibilityState: "hidden" as string,
};

const g = globalThis as typeof globalThis & {
  window?: unknown;
  document?: unknown;
};

g.window = {
  addEventListener: (type: string, listener: Listener) =>
    void windowListeners.set(type, listener),
};
g.document = {
  hasFocus: () => browser.focused,
  get visibilityState() {
    return browser.visibilityState;
  },
  addEventListener: (type: string, listener: Listener) =>
    void documentListeners.set(type, listener),
};

const { isAppActive } = await import("../src/lib/appVisibility.ts?env=browser");

test("without a DOM the app is assumed active (headless default)", () => {
  // The static import at the top ran with no document defined.
  assert.equal(isAppActiveHeadless(), true);
});

test("an unfocused, hidden browser tab is inactive from the start", () => {
  // windowListeners were registered during the dynamic import above.
  assert.deepEqual([...windowListeners.keys()].sort(), ["blur", "focus"]);
  assert.deepEqual([...documentListeners.keys()], ["visibilitychange"]);
  assert.equal(isAppActive(), false);
});

test("focus alone does not make a hidden tab active", () => {
  windowListeners.get("focus")!();
  assert.equal(isAppActive(), false);
});

test("becoming visible completes the activation", () => {
  browser.visibilityState = "visible";
  documentListeners.get("visibilitychange")!();
  assert.equal(isAppActive(), true);
});

test("losing focus deactivates an otherwise visible tab", () => {
  windowListeners.get("blur")!();
  assert.equal(isAppActive(), false);
  windowListeners.get("focus")!();
  assert.equal(isAppActive(), true);
});

test("switching back to a hidden tab deactivates again", () => {
  browser.visibilityState = "hidden";
  documentListeners.get("visibilitychange")!();
  assert.equal(isAppActive(), false);
  delete g.window;
  delete g.document;
});
