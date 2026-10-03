import { create } from "zustand";
import { persist } from "zustand/middleware";

interface UIState {
  sidebarCollapsed: boolean;
  sidebarMobileOpen: boolean;
  rightPanelOpen: boolean;
  rightPanelTab: "code" | "preview";
  mobileTab: "chat" | "files" | "terminal" | "preview";

  // Actions
  toggleSidebar: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  setSidebarMobileOpen: (open: boolean) => void;
  toggleRightPanel: () => void;
  setRightPanelOpen: (open: boolean) => void;
  setRightPanelTab: (tab: "code" | "preview") => void;
  setMobileTab: (tab: "chat" | "files" | "terminal" | "preview") => void;
}

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      sidebarMobileOpen: false,
      rightPanelOpen: false,
      rightPanelTab: "code",
      mobileTab: "chat",

      toggleSidebar: () =>
        set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
      setSidebarMobileOpen: (open) => set({ sidebarMobileOpen: open }),
      toggleRightPanel: () =>
        set((s) => ({ rightPanelOpen: !s.rightPanelOpen })),
      setRightPanelOpen: (open) => set({ rightPanelOpen: open }),
      setRightPanelTab: (tab) => set({ rightPanelTab: tab }),
      setMobileTab: (tab) => set({ mobileTab: tab }),
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
