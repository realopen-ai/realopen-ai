import { create } from "zustand";

export const useLearnStore = create<{
  studyDeckId: string | null;
  quizId: string | null;
  setQuiz: (id: string | null) => void;
  setStudyDeck: (id: string | null) => void;
}>((set) => ({
  studyDeckId: null,
  quizId: null,
  setQuiz: (quizId) => set({ quizId, studyDeckId: null }),
  setStudyDeck: (studyDeckId) => set({ studyDeckId, quizId: null }),
}));
