import { create } from "zustand";

interface UIState {
  sidebarCollapsed: boolean;
  sidebarMobileOpen: boolean;
  rightPanelOpen: boolean;
  rightPanelTab: "files" | "terminal";
  mobileTab: "chat" | "files" | "terminal";

  // Actions
  toggleSidebar: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  setSidebarMobileOpen: (open: boolean) => void;
  toggleRightPanel: () => void;
  setRightPanelOpen: (open: boolean) => void;
  setRightPanelTab: (tab: "files" | "terminal") => void;
  setMobileTab: (tab: "chat" | "files" | "terminal") => void;
}

export const useUIStore = create<UIState>((set) => ({
  sidebarCollapsed: false,
  sidebarMobileOpen: false,
  rightPanelOpen: true,
  rightPanelTab: "files",
  mobileTab: "chat",

  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
  setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
  setSidebarMobileOpen: (open) => set({ sidebarMobileOpen: open }),
  toggleRightPanel: () => set((s) => ({ rightPanelOpen: !s.rightPanelOpen })),
  setRightPanelOpen: (open) => set({ rightPanelOpen: open }),
  setRightPanelTab: (tab) => set({ rightPanelTab: tab }),
  setMobileTab: (tab) => set({ mobileTab: tab }),
}));
