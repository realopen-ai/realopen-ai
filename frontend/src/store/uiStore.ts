import { create } from "zustand";
import { persist } from "zustand/middleware";
import { sideActivityKey, type SideActivity } from "../lib/rightPanelTabs.ts";
import { useSandboxStore } from "./sandboxStore.ts";

type SideTab = { key: string; activity: SideActivity; routeKey: string };
type SideTabState = { sideTabs: SideTab[]; activeSideTab: string | null };

const sideTabsStorageKey = "realopen-ai-side-tabs";

function loadSideTabs(): SideTabState {
  try {
    const saved = window.sessionStorage.getItem(sideTabsStorageKey);
    if (saved) {
      const state: SideTabState = JSON.parse(saved);
      if (Array.isArray(state.sideTabs)) return state;
    }
  } catch {
    // The panel still works when browser storage is unavailable.
  }
  return { sideTabs: [], activeSideTab: null };
}

interface UIState extends SideTabState {
  sidebarCollapsed: boolean;
  sidebarMobileOpen: boolean;
  rightPanelOpen: boolean;
  rightPanelWidth: number | null;
  rightPanelTab: "code" | "preview";
  mobileTab: "chat" | "files" | "terminal" | "preview";

  // Actions
  toggleSidebar: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  setSidebarMobileOpen: (open: boolean) => void;
  toggleRightPanel: () => void;
  setRightPanelOpen: (open: boolean) => void;
  setRightPanelWidth: (percentage: number) => void;
  setRightPanelTab: (tab: "code" | "preview", background?: boolean) => void;
  setMobileTab: (tab: "chat" | "files" | "terminal" | "preview") => void;
  openSideTab: (
    activity: SideActivity,
    routeKey: string,
    openPanel?: boolean,
  ) => void;
  selectSideTab: (key: string) => void;
  closeSideTab: (key: string) => void;
  rememberQuizAttempt: (key: string, attemptId: string) => void;
  clearSideTabs: (routeKey: string) => void;
}

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      sidebarMobileOpen: false,
      rightPanelOpen: false,
      rightPanelWidth: null,
      rightPanelTab: "code",
      mobileTab: "chat",
      ...loadSideTabs(),

      toggleSidebar: () =>
        set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
      setSidebarMobileOpen: (open) => set({ sidebarMobileOpen: open }),
      toggleRightPanel: () =>
        set((s) => ({ rightPanelOpen: !s.rightPanelOpen })),
      setRightPanelOpen: (open) => set({ rightPanelOpen: open }),
      setRightPanelWidth: (percentage) => {
        if (Number.isFinite(percentage) && percentage > 0 && percentage <= 100)
          set((state) =>
            state.rightPanelWidth === percentage
              ? state
              : { rightPanelWidth: percentage },
          );
      },
      setRightPanelTab: (tab, background) =>
        set(
          background
            ? { rightPanelTab: tab }
            : { rightPanelTab: tab, activeSideTab: null },
        ),
      setMobileTab: (tab) => set({ mobileTab: tab }),
      openSideTab: (activity, routeKey, openPanel = true) =>
        set((state) => {
          const key = sideActivityKey(activity);
          const current = state.sideTabs.filter(
            (tab) => tab.routeKey === routeKey,
          );
          return {
            sideTabs: current.some((tab) => tab.key === key)
              ? current
              : [...current, { key, activity, routeKey }],
            activeSideTab: key,
            rightPanelOpen: openPanel || state.rightPanelOpen,
          };
        }),
      selectSideTab: (key) =>
        set((state) =>
          state.sideTabs.some((tab) => tab.key === key)
            ? { activeSideTab: key }
            : state,
        ),
      closeSideTab: (key) =>
        set((state) => {
          const index = state.sideTabs.findIndex((tab) => tab.key === key);
          if (index < 0) return state;
          const sideTabs = state.sideTabs.filter((tab) => tab.key !== key);
          if (state.activeSideTab !== key) return { sideTabs };
          const next = sideTabs[index - 1] ?? sideTabs[index];
          const { previewUrl, sandboxId, sandboxes } =
            useSandboxStore.getState();
          const sandbox = sandboxes.find((item) => item.id === sandboxId);
          return next
            ? { sideTabs, activeSideTab: next.key }
            : {
                sideTabs,
                activeSideTab: null,
                rightPanelTab:
                  previewUrl && (!sandbox || sandbox.status === "running")
                    ? "preview"
                    : "code",
              };
        }),
      clearSideTabs: (routeKey) =>
        set((state) =>
          state.sideTabs.some((tab) => tab.routeKey !== routeKey)
            ? {
                sideTabs: state.sideTabs.filter(
                  (tab) => tab.routeKey === routeKey,
                ),
                activeSideTab:
                  state.sideTabs.find(
                    (tab) =>
                      tab.routeKey === routeKey &&
                      tab.key === state.activeSideTab,
                  )?.key ?? null,
              }
            : state,
        ),
      rememberQuizAttempt: (key, attemptId) =>
        set((state) => {
          const tab = state.sideTabs.find((item) => item.key === key);
          if (
            tab?.activity.kind !== "quiz" ||
            tab.activity.attemptId === attemptId
          )
            return state;
          const activity = { ...tab.activity, attemptId };
          return {
            sideTabs: state.sideTabs.map((item) =>
              item === tab ? { ...item, activity } : item,
            ),
          };
        }),
    }),
    {
      name: "realopen-ai-ui",
      version: 1,
      partialize: ({
        sideTabs: _sideTabs,
        activeSideTab: _activeSideTab,
        ...state
      }) => state,
      migrate: (persisted) => {
        const state = persisted as Partial<UIState>;
        return {
          ...state,
          rightPanelTab: state.rightPanelTab === "preview" ? "preview" : "code",
        } as UIState;
      },
    },
  ),
);

useUIStore.subscribe((state, previous) => {
  if (
    state.sideTabs === previous.sideTabs &&
    state.activeSideTab === previous.activeSideTab
  )
    return;
  try {
    if (state.sideTabs.length) {
      window.sessionStorage.setItem(
        sideTabsStorageKey,
        JSON.stringify({
          sideTabs: state.sideTabs,
          activeSideTab: state.activeSideTab,
        }),
      );
    } else window.sessionStorage.removeItem(sideTabsStorageKey);
  } catch {
    // Keep the live state usable when storage is full or unavailable.
  }
});
