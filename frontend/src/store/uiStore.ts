import { create } from "zustand";
import { persist } from "zustand/middleware";

interface UIState {
  sidebarCollapsed: boolean;
  sidebarMobileOpen: boolean;
  rightPanelOpen: boolean;
  rightPanelTab: "code" | "preview";
  mobileTab: "chat" | "files" | "terminal" | "preview";
  showBrainPage: boolean;
  showWorkspacePage: boolean;

  // Actions
  toggleSidebar: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  setSidebarMobileOpen: (open: boolean) => void;
  toggleRightPanel: () => void;
  setRightPanelOpen: (open: boolean) => void;
  setRightPanelTab: (tab: "code" | "preview") => void;
  setMobileTab: (tab: "chat" | "files" | "terminal" | "preview") => void;
  setShowBrainPage: (show: boolean) => void;
  setShowWorkspacePage: (show: boolean) => void;
}

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      sidebarMobileOpen: false,
      rightPanelOpen: false,
      rightPanelTab: "code",
      mobileTab: "chat",
      showBrainPage: false,
      showWorkspacePage: false,

      toggleSidebar: () =>
        set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
      setSidebarMobileOpen: (open) => set({ sidebarMobileOpen: open }),
      toggleRightPanel: () =>
        set((s) => ({ rightPanelOpen: !s.rightPanelOpen })),
      setRightPanelOpen: (open) => set({ rightPanelOpen: open }),
      setRightPanelTab: (tab) => set({ rightPanelTab: tab }),
      setMobileTab: (tab) => set({ mobileTab: tab }),
      setShowBrainPage: (show) =>
        set({
          showBrainPage: show,
          ...(show ? { showWorkspacePage: false } : {}),
        }),
      setShowWorkspacePage: (show) =>
        set({
          showWorkspacePage: show,
          ...(show ? { showBrainPage: false } : {}),
        }),
    }),
    {
      name: "realopen-ai-ui",
      version: 1,
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
