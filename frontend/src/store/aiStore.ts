import { create } from "zustand";

import {
  fetchAvailableModels,
  fetchProviders,
  setModelPreference,
  connectGroqProvider,
  disconnectGroqProvider,
  type AvailableModel,
  type ModelTaskSlot,
  type ProviderInfo,
} from "@/api/client";
import { useChatStore } from "@/store/chatStore";
import { dbgError } from "@/lib/debug";

// ─── Store ───────────────────────────────────────────────────────

interface AiState {
  // Model dropdown data
  models: AvailableModel[];
  tasks: ModelTaskSlot[];
  groqConnected: boolean;
  ollamaConnected: boolean;
  isLoadingModels: boolean;

  // Provider data
  providers: ProviderInfo[];
  isLoadingProviders: boolean;

  // Groq connect flow
  isConnectingGroq: boolean;
  groqError: string | null;

  // Model preference save feedback: task → error message (null = ok)
  taskErrors: Record<string, string | null>;

  // Actions
  load: () => Promise<void>;
  loadProviders: () => Promise<void>;
  selectModel: (task: string, model: string | null) => Promise<void>;
  connectGroq: (apiKey: string) => Promise<boolean>;
  disconnectGroq: () => Promise<void>;
}

export const useAiStore = create<AiState>((set, get) => ({
  models: [],
  tasks: [],
  groqConnected: false,
  ollamaConnected: false,
  isLoadingModels: false,

  providers: [],
  isLoadingProviders: false,

  isConnectingGroq: false,
  groqError: null,

  taskErrors: {},

  load: async () => {
    set({ isLoadingModels: true });
    const data = await fetchAvailableModels();
    if (data) {
      set({
        models: data.models,
        tasks: data.tasks,
        groqConnected: data.groq_connected,
        ollamaConnected: data.ollama_connected,
        isLoadingModels: false,
      });
    } else {
      set({ isLoadingModels: false });
    }
    // Providers and models overlap (groq_connected) — refresh both for
    // a consistent view.
    await get().loadProviders();
  },

  loadProviders: async () => {
    set({ isLoadingProviders: true });
    const providers = await fetchProviders();
    set({ providers, isLoadingProviders: false });
  },

  selectModel: async (task, model) => {
    // Optimistic update
    set((s) => ({
      tasks: s.tasks.map((t) =>
        t.task === task
          ? {
              ...t,
              model: model ?? t.default_model,
              is_default: model === null,
            }
          : t,
      ),
      taskErrors: { ...s.taskErrors, [task]: null },
    }));

    const row = await setModelPreference(task, model);
    if (!row) {
      // Save failed — reload server state and surface an error
      dbgError(`selectModel failed for task=${task}`);
      set((s) => ({
        taskErrors: { ...s.taskErrors, [task]: "saveFailed" },
      }));
      await get().load();
      return;
    }

    set((s) => ({
      tasks: s.tasks.map((t) => (t.task === task ? row : t)),
    }));

    // The Chat/Agent slot drives the active conversation model — keep
    // the chat store in sync so the next message uses the new model.
    if (task === "chat" && row.model) {
      useChatStore.getState().setSelectedModel(row.model);
    }
  },

  connectGroq: async (apiKey) => {
    set({ isConnectingGroq: true, groqError: null });
    const result = await connectGroqProvider(apiKey);
    set({
      isConnectingGroq: false,
      groqError: result.ok ? null : (result.error ?? "Connection failed"),
    });
    if (result.ok) {
      // Groq models become selectable in every (non local-only) slot
      await get().load();
    }
    return result.ok;
  },

  disconnectGroq: async () => {
    const ok = await disconnectGroqProvider();
    if (ok) {
      await get().load();
    }
    return;
  },
}));
