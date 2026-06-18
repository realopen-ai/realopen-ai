import { create } from "zustand";
import {
  fetchMemories as apiFetchMemories,
  fetchMemoryCategories as apiFetchCategories,
  addMemory as apiAddMemory,
  updateMemory as apiUpdateMemory,
  deleteMemory as apiDeleteMemory,
  pinMemory as apiPinMemory,
  searchMemories as apiSearchMemories,
  auditMemories as apiAuditMemories,
  type MemoryItem,
  type CategoryCount,
} from "@/api/memoryClient";

// ─── Store ───────────────────────────────────────────────────────

interface MemoryState {
  memories: MemoryItem[];
  categories: CategoryCount[];
  isLoading: boolean;
  isSearching: boolean;
  isAuditing: boolean;
  activeCategory: string; // "all" or a specific category
  searchQuery: string;
  searchResults: MemoryItem[];
  isExtracting: boolean; // true when background extraction is happening
  lastExtraction: { count: number; timestamp: number } | null;

  // Actions
  loadMemories: () => Promise<void>;
  loadCategories: () => Promise<void>;
  addMemory: (text: string, category: string) => Promise<void>;
  updateMemory: (id: string, text: string, category?: string) => Promise<void>;
  deleteMemory: (id: string) => Promise<void>;
  pinMemory: (id: string, pinned: boolean) => Promise<void>;
  searchMemories: (query: string, category?: string) => Promise<void>;
  setActiveCategory: (category: string) => void;
  clearSearch: () => void;
  auditMemories: () => Promise<void>;
  setExtracting: (extracting: boolean) => void;
  setLastExtraction: (count: number) => void;
  clearLastExtraction: () => void;
}

export const useMemoryStore = create<MemoryState>((set, get) => ({
  memories: [],
  categories: [],
  isLoading: false,
  isSearching: false,
  isAuditing: false,
  activeCategory: "all",
  searchQuery: "",
  searchResults: [],
  isExtracting: false,
  lastExtraction: null,

  loadMemories: async () => {
    set({ isLoading: true });
    try {
      const category =
        get().activeCategory === "all" ? undefined : get().activeCategory;
      const memories = await apiFetchMemories(category);
      set({ memories, isLoading: false });
    } catch {
      set({ isLoading: false });
    }
  },

  loadCategories: async () => {
    try {
      const categories = await apiFetchCategories();
      set({ categories });
    } catch {
      /* ignore */
    }
  },

  addMemory: async (text, category) => {
    try {
      const memory = await apiAddMemory(text, category, "user");
      if (memory) {
        set((s) => ({
          memories: [memory, ...s.memories],
        }));
        // Refresh categories
        const categories = await apiFetchCategories();
        set({ categories });
      }
    } catch {
      /* ignore */
    }
  },

  updateMemory: async (id, text, category) => {
    try {
      const success = await apiUpdateMemory(id, text, category);
      if (success) {
        set((s) => ({
          memories: s.memories.map((m) =>
            m.id === id
              ? {
                  ...m,
                  text,
                  ...(category ? { category } : {}),
                  updated_at: Date.now(),
                }
              : m,
          ),
        }));
      }
    } catch {
      /* ignore */
    }
  },

  deleteMemory: async (id) => {
    // Optimistically remove
    set((s) => ({
      memories: s.memories.filter((m) => m.id !== id),
      searchResults: s.searchResults.filter((m) => m.id !== id),
    }));
    try {
      await apiDeleteMemory(id);
      // Refresh categories
      const categories = await apiFetchCategories();
      set({ categories });
    } catch {
      /* ignore */
    }
  },

  pinMemory: async (id, pinned) => {
    // Optimistically update
    set((s) => ({
      memories: s.memories.map((m) => (m.id === id ? { ...m, pinned } : m)),
      searchResults: s.searchResults.map((m) =>
        m.id === id ? { ...m, pinned } : m,
      ),
    }));
    try {
      await apiPinMemory(id, pinned);
    } catch {
      // Revert on error
      set((s) => ({
        memories: s.memories.map((m) =>
          m.id === id ? { ...m, pinned: !pinned } : m,
        ),
        searchResults: s.searchResults.map((m) =>
          m.id === id ? { ...m, pinned: !pinned } : m,
        ),
      }));
    }
  },

  searchMemories: async (query, category) => {
    set({ isSearching: true, searchQuery: query });
    try {
      const results = await apiSearchMemories(query, category);
      set({ searchResults: results, isSearching: false });
    } catch {
      set({ searchResults: [], isSearching: false });
    }
  },

  setActiveCategory: (category) => {
    set({ activeCategory: category });
  },

  clearSearch: () => {
    set({ searchQuery: "", searchResults: [] });
  },

  auditMemories: async () => {
    set({ isAuditing: true });
    try {
      const result = await apiAuditMemories();
      if (result) {
        // Reload memories and categories after audit
        await get().loadMemories();
        await get().loadCategories();
      }
      set({ isAuditing: false });
    } catch {
      set({ isAuditing: false });
    }
  },

  setExtracting: (extracting) => {
    set({ isExtracting: extracting });
  },

  setLastExtraction: (count) => {
    set({ lastExtraction: { count, timestamp: Date.now() } });
  },

  clearLastExtraction: () => {
    set({ lastExtraction: null });
  },
}));
