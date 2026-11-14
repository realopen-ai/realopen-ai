import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { SourceLocation } from "@/lib/sourceNavigation";

interface UIState {
  sidebarCollapsed: boolean;
  sidebarMobileOpen: boolean;
  rightPanelOpen: boolean;
  rightPanelTab: "code" | "preview";
  mobileTab: "chat" | "files" | "terminal" | "preview";
  citationPreview: { source: SourceLocation; routeKey: string } | null;

  // Actions
  toggleSidebar: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  setSidebarMobileOpen: (open: boolean) => void;
  toggleRightPanel: () => void;
  setRightPanelOpen: (open: boolean) => void;
  setRightPanelTab: (tab: "code" | "preview") => void;
  setMobileTab: (tab: "chat" | "files" | "terminal" | "preview") => void;
  setCitationPreview: (preview: UIState["citationPreview"]) => void;
}

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      sidebarMobileOpen: false,
      rightPanelOpen: false,
      rightPanelTab: "code",
      mobileTab: "chat",
      citationPreview: null,

      toggleSidebar: () =>
        set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
      setSidebarMobileOpen: (open) => set({ sidebarMobileOpen: open }),
      toggleRightPanel: () =>
        set((s) => ({ rightPanelOpen: !s.rightPanelOpen })),
      setRightPanelOpen: (open) => set({ rightPanelOpen: open }),
      setRightPanelTab: (tab) => set({ rightPanelTab: tab }),
      setMobileTab: (tab) => set({ mobileTab: tab }),
      setCitationPreview: (preview) => set({ citationPreview: preview }),
    }),
    {
      name: "realopen-ai-ui",
      version: 1,
      partialize: ({ citationPreview: _citationPreview, ...state }) => state,
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
