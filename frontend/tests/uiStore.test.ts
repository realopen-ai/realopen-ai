/**
 * Tests for src/store/uiStore.ts — the persisted UI-layout zustand store.
 *
 * The store uses zustand/persist, whose default storage is
 * `window.localStorage` — created at store-evaluation time. Under Node there
 * is no window, so a fake localStorage is installed BEFORE the dynamic
 * import (the same pattern as the alias hooks in tests/helpers/viteCompat.ts:
 * imports must be dynamic so the globals exist when the module evaluates).
 * With the fake in place the full persist machinery runs for real:
 * hydration on import, {state, version} writes on every set, migrations on
 * version mismatch. No `@/` specifiers in this module's graph, so no
 * viteCompat import is needed here.
 */
import assert from "node:assert/strict";
import test from "node:test";

// ── Fake localStorage (must exist before the store evaluates) ─────────
const storage = new Map<string, string>();
const session = new Map<string, string>();
const g = globalThis as typeof globalThis & { window?: unknown };
g.window = {
  localStorage: {
    getItem: (key: string) => (storage.has(key) ? storage.get(key)! : null),
    setItem: (key: string, value: string) => void storage.set(key, value),
    removeItem: (key: string) => void storage.delete(key),
  },
  sessionStorage: {
    getItem: (key: string) => session.get(key) ?? null,
    setItem: (key: string, value: string) => void session.set(key, value),
    removeItem: (key: string) => void session.delete(key),
  },
};

const { useUIStore } = await import("../src/store/uiStore.ts");
const { useSandboxStore } = await import("../src/store/sandboxStore.ts");

test("fresh store starts from the documented defaults", () => {
  const s = useUIStore.getState();
  assert.equal(s.sidebarCollapsed, false);
  assert.equal(s.sidebarMobileOpen, false);
  assert.equal(s.rightPanelOpen, false);
  assert.equal(s.rightPanelTab, "code");
  assert.equal(s.mobileTab, "chat");
});

test("every layout action updates its slice", () => {
  const s = useUIStore.getState();
  s.toggleSidebar();
  s.setSidebarCollapsed(false);
  s.toggleSidebar();
  assert.equal(useUIStore.getState().sidebarCollapsed, true);

  s.setSidebarMobileOpen(true);
  assert.equal(useUIStore.getState().sidebarMobileOpen, true);

  s.toggleRightPanel();
  s.setRightPanelOpen(false);
  s.toggleRightPanel();
  assert.equal(useUIStore.getState().rightPanelOpen, true);

  s.setRightPanelTab("preview");
  assert.equal(useUIStore.getState().rightPanelTab, "preview");

  s.setMobileTab("terminal");
  assert.equal(useUIStore.getState().mobileTab, "terminal");
});

test("state changes are persisted under the realopen-ai-ui key", () => {
  useUIStore.getState().setSidebarCollapsed(true);
  useUIStore.getState().setMobileTab("preview");

  const raw = storage.get("realopen-ai-ui");
  assert.ok(raw, "expected a localStorage write");
  const parsed = JSON.parse(raw) as {
    state: Record<string, unknown>;
    version: number;
  };
  assert.equal(parsed.version, 1);
  // Actions are functions and are dropped by JSON — only data persists.
  assert.equal(parsed.state.sidebarCollapsed, true);
  assert.equal(parsed.state.mobileTab, "preview");
  assert.equal(typeof parsed.state.toggleSidebar, "undefined");
});

test("side tabs stack, survive collapse and restore the preceding activity on close", () => {
  const ui = useUIStore.getState();
  ui.setRightPanelOpen(false);
  ui.openSideTab({ kind: "quiz", id: "math", title: "Math" }, "chat");
  assert.equal(useUIStore.getState().rightPanelOpen, true);
  assert.equal(useUIStore.getState().activeSideTab, "quiz:math");
  ui.toggleRightPanel();
  ui.toggleRightPanel();
  assert.equal(useUIStore.getState().activeSideTab, "quiz:math");
  ui.openSideTab(
    { kind: "citation", source: { documentId: "doc", page: 5 } },
    "chat",
  );
  const citation = useUIStore.getState().activeSideTab!;
  assert.deepEqual(
    useUIStore.getState().sideTabs.map((tab) => tab.key),
    ["quiz:math", citation],
  );
  ui.closeSideTab(citation);
  assert.equal(useUIStore.getState().activeSideTab, "quiz:math");
  useSandboxStore.getState().setPreviewUrl("http://localhost:6767");
  ui.closeSideTab("quiz:math");
  assert.equal(useUIStore.getState().activeSideTab, null);
  assert.equal(useUIStore.getState().rightPanelTab, "preview");
  useSandboxStore.getState().setPreviewUrl(null);
  ui.openSideTab({ kind: "flashcards", id: "deck", title: "Deck" }, "chat");
  ui.closeSideTab("flashcards:deck");
  assert.equal(useUIStore.getState().rightPanelTab, "code");
});

test("side tabs reopen existing content and clear on route change", () => {
  const ui = useUIStore.getState();
  const activity = {
    kind: "citation",
    source: { documentId: "doc", excerpt: "Source text" },
  } as const;
  ui.setRightPanelOpen(false);
  ui.openSideTab(activity, "chat", false);
  assert.equal(useUIStore.getState().rightPanelOpen, false);
  ui.openSideTab(activity, "chat");
  assert.equal(useUIStore.getState().sideTabs.length, 1);
  ui.setRightPanelTab("preview", true);
  assert.equal(useUIStore.getState().activeSideTab, "document:doc::");
  ui.setRightPanelTab("code");
  assert.equal(useUIStore.getState().activeSideTab, null);
  ui.selectSideTab("document:doc::");
  assert.equal(useUIStore.getState().activeSideTab, "document:doc::");
  const persisted = JSON.parse(storage.get("realopen-ai-ui")!);
  assert.equal("sideTabs" in persisted.state, false);
  assert.equal("activeSideTab" in persisted.state, false);
  ui.clearSideTabs("other-chat");
  assert.deepEqual(useUIStore.getState().sideTabs, []);
  assert.equal(useUIStore.getState().activeSideTab, null);
  assert.equal(session.has("realopen-ai-side-tabs"), false);
});

test("reload restores the tab order, selection, collapsed panel and quiz attempt", async () => {
  const ui = useUIStore.getState();
  ui.openSideTab({ kind: "quiz", id: "math", title: "Math" }, "/chat:default");
  ui.rememberQuizAttempt("quiz:math", "attempt-1");
  ui.openSideTab(
    {
      kind: "citation",
      source: {
        artifact: { artifact_id: "slides", version: 2, section_id: "slide-4" },
      },
    },
    "/chat:default",
  );
  ui.setRightPanelOpen(false);
  // A fresh module instance creates a new store against the same browser storage.
  const moduleUrl = new URL("../src/store/uiStore.ts?reload", import.meta.url);
  const { useUIStore: restored }: { useUIStore: typeof useUIStore } =
    await import(moduleUrl.href);
  const state = restored.getState();
  assert.equal(state.rightPanelOpen, false);
  assert.equal(state.activeSideTab, "artifact:slides:2:slide-4");
  assert.deepEqual(
    state.sideTabs.map((tab) => tab.key),
    ["quiz:math", "artifact:slides:2:slide-4"],
  );
  assert.deepEqual(state.sideTabs[0].activity, {
    kind: "quiz",
    id: "math",
    title: "Math",
    attemptId: "attempt-1",
  });
  state.clearSideTabs("/chat:default");
  state.closeSideTab("artifact:slides:2:slide-4");
  assert.equal(restored.getState().activeSideTab, "quiz:math");
  state.closeSideTab("quiz:math");
  assert.equal(session.has("realopen-ai-side-tabs"), false);
  ui.clearSideTabs("/other:default");
});

test("a reload rehydrates the persisted layout and keeps actions working", async () => {
  storage.set(
    "realopen-ai-ui",
    JSON.stringify({
      state: {
        sidebarCollapsed: true,
        rightPanelTab: "preview",
        mobileTab: "terminal",
      },
      version: 1,
    }),
  );
  await useUIStore.persist.rehydrate();

  const s = useUIStore.getState();
  assert.equal(s.sidebarCollapsed, true);
  assert.equal(s.rightPanelTab, "preview");
  assert.equal(s.mobileTab, "terminal");
  // Rehydration merges over the live store — actions survive.
  s.toggleSidebar();
  assert.equal(useUIStore.getState().sidebarCollapsed, false);
});

test("a collapsed panel retains its last positive width after reload", async () => {
  const ui = useUIStore.getState();
  ui.setRightPanelOpen(true);
  ui.setRightPanelWidth(48.25);
  ui.setRightPanelOpen(false);
  ui.setRightPanelWidth(0);
  const moduleUrl = new URL(
    "../src/store/uiStore.ts?width-reload",
    import.meta.url,
  );
  const { useUIStore: restored }: { useUIStore: typeof useUIStore } =
    await import(moduleUrl.href);
  assert.equal(restored.getState().rightPanelOpen, false);
  assert.equal(restored.getState().rightPanelWidth, 48.25);
});

test("legacy persisted state is migrated to the current version", async () => {
  storage.set(
    "realopen-ai-ui",
    JSON.stringify({
      state: { sidebarMobileOpen: true, rightPanelTab: "preview" },
      version: 0,
    }),
  );
  await useUIStore.persist.rehydrate();

  const s = useUIStore.getState();
  assert.equal(s.sidebarMobileOpen, true);
  assert.equal(s.rightPanelTab, "preview");
  // Migration persisted the upgraded entry back at the current version.
  const reparsed = JSON.parse(storage.get("realopen-ai-ui")!) as {
    version: number;
  };
  assert.equal(reparsed.version, 1);
});

test("the migrate function coerces unknown rightPanelTab values to code", () => {
  const migrate = useUIStore.persist.getOptions().migrate;
  assert.ok(migrate, "uiStore must declare a migrate function");

  assert.equal(
    (
      migrate({ rightPanelTab: "preview", mobileTab: "chat" } as never, 0) as {
        rightPanelTab: string;
      }
    ).rightPanelTab,
    "preview",
  );
  // Anything that is not exactly "preview" is not a valid tab — the
  // migration clamps it to the default tab instead of crashing later.
  for (const bad of ["code", "notes", "", null, undefined]) {
    assert.equal(
      (migrate({ rightPanelTab: bad } as never, 0) as { rightPanelTab: string })
        .rightPanelTab,
      "code",
      `expected ${String(bad)} to be clamped`,
    );
  }
  // Non-tab fields pass through untouched.
  const migrated = migrate({ sidebarCollapsed: true } as never, 0) as {
    sidebarCollapsed: boolean;
  };
  assert.equal(migrated.sidebarCollapsed, true);
});
