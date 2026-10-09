import { create } from "zustand";
import type { Notebook } from "@/api/notebooksClient";

export const useNotebookStore = create<{
  notebook: Notebook | null;
  mobileTab: "sources" | "chat" | "studio";
  setNotebook: (notebook: Notebook | null) => void;
  setMobileTab: (mobileTab: "sources" | "chat" | "studio") => void;
}>((set) => ({
  notebook: null,
  mobileTab: "chat",
  setNotebook: (notebook) => set({ notebook }),
  setMobileTab: (mobileTab) => set({ mobileTab }),
}));
