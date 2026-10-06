import { create } from "zustand";

export const useLearnStore = create<{
  studyDeckId: string | null;
  setStudyDeck: (id: string | null) => void;
}>((set) => ({
  studyDeckId: null,
  setStudyDeck: (studyDeckId) => set({ studyDeckId }),
}));
