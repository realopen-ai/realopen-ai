import { create } from "zustand";
import {
  fetchConversations,
  createConversation as apiCreateConversation,
  deleteConversation as apiDeleteConversation,
  fetchConversationMessages,
  fetchConversationDetail,
  fetchModules as apiFetchModules,
  toggleModule as apiToggleModule,
  type ConversationDTO,
  type MessageDTO,
  type ModuleInfo,
} from "@/api/client";
import type { RetrievedSourceDTO } from "@/api/documentsClient";

// ─── Types ───────────────────────────────────────────────────────

export interface ToolCallResult {
  id: string;
  type:
    | "websearch"
    | "vision"
    | "code_exec"
    | "file_read"
    | "file_write"
    | "deepsearch"
    | "image_gen";
  status: "running" | "completed" | "error";
  title: string;
  startedAt: number;
  completedAt?: number;
  // Web search
  query?: string;
  webResults?: { title: string; url: string; snippet: string }[];
  // Code exec
  language?: string;
  code?: string;
  output?: string;
  exitCode?: number;
  // Vision
  imageDescription?: string;
  // Deep search
  steps?: { label: string; status: "pending" | "running" | "done" }[];
  // Media generation
  genResults?: { type: string; data: string; filename?: string }[];
  // Files
  filePath?: string;
  fileContent?: string;
  // Error
  error?: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  model?: string;
  toolCalls: ToolCallResult[];
  sandboxOpen: boolean;
  isStreaming: boolean;
  createdAt: number;
  completedAt?: number;
  shrugOverlay?: boolean;
  // Image / document info
  hasImage?: boolean;
  hasDocument?: boolean;
  imageCount?: number;
  documentCount?: number;
  // Thinking / reasoning
  thinking?: string;
  thinkingDuration?: number; // seconds
  generationDuration?: number; // seconds
  isThinking?: boolean;
  // RAG sources — only populated when the agent's rag_search tool fires
  sources?: RetrievedSourceDTO[];
  // Document digestion progress — populated when the user uploads docs
  // via chat and the backend streams document_digest_* SSE events. The
  // frontend renders an inline digestion progress indicator on the
  // user's message bubble while these are present.
  digestProgress?: DigestProgressItem[];
}

/**
 * One document digestion progress event for a chat-uploaded document.
 *
 * The backend emits `document_digest_progress`, `document_digest_done`,
 * and `document_digest_error` SSE events while digesting docs uploaded
 * via the chat input. We attach these to the user's message so the UI
 * can show real-time progress inline in the conversation — not just in
 * the right-panel terminal.
 */
export interface DigestProgressItem {
  filename: string;
  stage:
    | "started"
    | "extracting_text"
    | "extracting_images"
    | "chunking"
    | "describing_images"
    | "embedding"
    | "persisting"
    | "done"
    | "error";
  percent: number;
  details: string;
  documentId?: string | null;
  totalChunks?: number;
  totalImages?: number;
  error?: string;
}

export interface Conversation {
  id: string;
  title: string;
  messages: Message[];
  model: string;
  createdAt: number;
  updatedAt: number;
}

export interface ModelOption {
  id: string;
  type: string;
  role: string;
  description: string;
  size: string;
}

// ─── Store ───────────────────────────────────────────────────────

interface ChatState {
  conversations: Conversation[];
  activeConversationId: string | null;
  models: ModelOption[];
  selectedModel: string;
  profileName: string;
  profileLabel: string;
  isStreaming: boolean;
  isLoadingConversations: boolean;

  // Module state
  modules: ModuleInfo[];
  isLoadingModules: boolean;

  // Actions
  loadConversations: () => Promise<void>;
  createConversation: () => Promise<string>;
  deleteConversation: (id: string) => Promise<void>;
  setActiveConversation: (id: string | null) => void;
  loadMessages: (conversationId: string) => Promise<void>;
  loadConversationById: (id: string) => Promise<boolean>;
  addMessage: (
    conversationId: string,
    message: Omit<
      Message,
      "id" | "createdAt" | "toolCalls" | "sandboxOpen" | "isStreaming"
    > & { shrugOverlay?: boolean },
  ) => string;
  updateMessage: (
    conversationId: string,
    messageId: string,
    updates: Partial<Message>,
  ) => void;
  appendToMessage: (
    conversationId: string,
    messageId: string,
    content: string,
  ) => void;
  appendToThinking: (
    conversationId: string,
    messageId: string,
    thinking: string,
  ) => void;
  setThinkingState: (
    conversationId: string,
    messageId: string,
    isThinking: boolean,
  ) => void;
  setThinkingDuration: (
    conversationId: string,
    messageId: string,
    duration: number,
  ) => void;
  setGenerationDuration: (
    conversationId: string,
    messageId: string,
    duration: number,
  ) => void;
  addToolCall: (
    conversationId: string,
    messageId: string,
    toolCall: Omit<ToolCallResult, "id" | "startedAt">,
  ) => string;
  updateToolCall: (
    conversationId: string,
    messageId: string,
    toolCallId: string,
    updates: Partial<ToolCallResult>,
  ) => void;
  toggleSandbox: (conversationId: string, messageId: string) => void;
  openSandbox: (conversationId: string, messageId: string) => void;
  setStreaming: (
    conversationId: string,
    messageId: string,
    streaming: boolean,
  ) => void;
  setModels: (models: ModelOption[]) => void;
  setProfileName: (name: string) => void;
  setProfileLabel: (label: string) => void;
  setSelectedModel: (model: string) => void;

  // RAG sources — append sources to a message when the agent's rag_search
  // tool fires. Called from stream.ts when a `rag_sources` SSE event arrives.
  addSources: (
    conversationId: string,
    messageId: string,
    sources: RetrievedSourceDTO[],
  ) => void;

  // RAG digestion progress — append a progress item to the user's
  // message when a `document_digest_*` SSE event arrives during chat
  // upload. The MessageBubble renders an inline progress indicator.
  addDigestProgress: (
    conversationId: string,
    messageId: string,
    item: DigestProgressItem,
  ) => void;

  // Module actions
  loadModules: () => Promise<void>;
  toggleModule: (moduleName: string, enabled: boolean) => Promise<boolean>;
  setModules: (modules: ModuleInfo[]) => void;

  // Computed
  getActiveConversation: () => Conversation | undefined;
  getActiveMessages: () => Message[];
}

let msgCounter = 0;
const genId = () => `msg-${Date.now()}-${++msgCounter}`;
const genToolId = () => `tool-${Date.now()}-${++msgCounter}`;

/** Convert a backend ConversationDTO to the frontend Conversation shape */
function dtoToConversation(dto: ConversationDTO): Conversation {
  return {
    id: dto.id,
    title: dto.title ?? "New Chat",
    messages: [], // Messages are loaded lazily when a conversation is selected
    model: dto.model ?? "default",
    createdAt: dto.createdAt,
    updatedAt: dto.updatedAt,
  };
}

/** Convert a backend MessageDTO to the frontend Message shape */
function dtoToMessage(dto: MessageDTO): Message {
  // Convert backend tool calls to frontend ToolCallResult format
  const toolCalls: ToolCallResult[] = (dto.toolCalls ?? []).map((tc, i) => ({
    id: tc.id ?? `restored-tc-${i}`,
    type: (tc.type as ToolCallResult["type"]) ?? "websearch",
    status: (tc.status as ToolCallResult["status"]) ?? "completed",
    title: tc.title ?? tc.type ?? "Tool",
    startedAt: dto.createdAt,
    completedAt: tc.completedAt,
    query: tc.query,
    webResults: tc.webResults,
    genResults: tc.genResults,
    language: tc.language,
    code: tc.code,
    output: tc.output,
    exitCode: tc.exitCode,
    imageDescription: tc.imageDescription,
    error: tc.error,
  }));

  return {
    id: dto.id,
    role: dto.role,
    content: dto.content,
    model: dto.model ?? undefined,
    toolCalls,
    sandboxOpen: true,
    isStreaming: false,
    createdAt: dto.createdAt,
    hasImage: dto.hasImage,
    hasDocument: dto.hasDocument,
    imageCount: dto.imageCount,
    documentCount: dto.documentCount,
    thinking: dto.thinking || undefined,
    thinkingDuration: dto.thinkingDuration,
    generationDuration: dto.generationDuration,
    isThinking: false,
  };
}

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [],
  activeConversationId: null,
  models: [],
  selectedModel: "default",
  profileName: "",
  profileLabel: "",
  isStreaming: false,
  isLoadingConversations: false,
  modules: [],
  isLoadingModules: false,

  loadConversations: async () => {
    set({ isLoadingConversations: true });
    try {
      const dtos = await fetchConversations();
      const newConvs = dtos.map(dtoToConversation);
      set((s) => ({
        conversations: newConvs.map((newConv) => {
          // Preserve messages from existing conversations in store
          const existing = s.conversations.find((c) => c.id === newConv.id);
          if (existing && existing.messages.length > 0) {
            return { ...newConv, messages: existing.messages };
          }
          return newConv;
        }),
        isLoadingConversations: false,
      }));
    } catch {
      set({ isLoadingConversations: false });
    }
  },

  createConversation: async () => {
    const state = get();
    try {
      const dto = await apiCreateConversation("New Chat", state.selectedModel);
      if (dto) {
        const conv = dtoToConversation(dto);
        set((s) => ({
          conversations: [conv, ...s.conversations],
          activeConversationId: conv.id,
        }));
        return conv.id;
      }
    } catch {
      /* fallback below */
    }
    // Fallback: create a local-only conversation
    const id = `local-${Date.now()}`;
    const conv: Conversation = {
      id,
      title: "New Chat",
      messages: [],
      model: get().selectedModel,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    set((s) => ({
      conversations: [conv, ...s.conversations],
      activeConversationId: id,
    }));
    return id;
  },

  deleteConversation: async (id) => {
    // Optimistically remove from local state
    set((s) => ({
      conversations: s.conversations.filter((c) => c.id !== id),
      // Always clear active when deleting the active conversation.
      // The Sidebar's handleDelete will navigate to "/" to match.
      activeConversationId:
        s.activeConversationId === id ? null : s.activeConversationId,
    }));
    // Delete from backend
    await apiDeleteConversation(id);
  },

  setActiveConversation: (id) => {
    set({ activeConversationId: id });
    // Load messages if not yet loaded
    if (id) {
      const conv = get().conversations.find((c) => c.id === id);
      if (conv && conv.messages.length === 0) {
        get().loadMessages(id);
      }
    }
  },

  loadMessages: async (conversationId) => {
    try {
      const dtos = await fetchConversationMessages(conversationId);
      const messages = dtos.map(dtoToMessage);
      set((s) => ({
        conversations: s.conversations.map((c) =>
          c.id === conversationId ? { ...c, messages } : c,
        ),
      }));
    } catch {
      /* ignore - messages will stay empty */
    }
  },

  /**
   * Load a conversation by ID from the backend (for direct URL access).
   * Returns true if the conversation was found, false if not (404).
   */
  loadConversationById: async (id: string) => {
    try {
      const detail = await fetchConversationDetail(id);
      if (!detail) return false;

      const conv: Conversation = {
        ...dtoToConversation(detail.conversation),
        messages: detail.messages.map(dtoToMessage),
      };

      set((s) => {
        const exists = s.conversations.find((c) => c.id === conv.id);
        if (exists) {
          // Update messages on existing conversation (preserve if already loaded)
          return {
            conversations: s.conversations.map((c) =>
              c.id === conv.id
                ? c.messages.length > 0
                  ? c // Keep existing messages if already loaded
                  : { ...c, messages: conv.messages }
                : c,
            ),
            activeConversationId: conv.id,
          };
        }
        // Add new conversation to the list
        return {
          conversations: [conv, ...s.conversations],
          activeConversationId: conv.id,
        };
      });
      return true;
    } catch {
      return false;
    }
  },

  addMessage: (conversationId, message) => {
    const msgId = genId();
    const msg: Message = {
      ...message,
      id: msgId,
      toolCalls: [],
      sandboxOpen: false,
      isStreaming: false,
      createdAt: Date.now(),
      shrugOverlay: message.shrugOverlay,
    };
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? { ...c, messages: [...c.messages, msg], updatedAt: Date.now() }
          : c,
      ),
    }));
    return msgId;
  },

  updateMessage: (conversationId, messageId, updates) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId ? { ...m, ...updates } : m,
              ),
            }
          : c,
      ),
    }));
  },

  appendToMessage: (conversationId, messageId, content) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId ? { ...m, content: m.content + content } : m,
              ),
            }
          : c,
      ),
    }));
  },

  appendToThinking: (conversationId, messageId, thinking) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId
                  ? { ...m, thinking: (m.thinking ?? "") + thinking }
                  : m,
              ),
            }
          : c,
      ),
    }));
  },

  setThinkingState: (conversationId, messageId, isThinking) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId ? { ...m, isThinking } : m,
              ),
            }
          : c,
      ),
    }));
  },

  setThinkingDuration: (conversationId, messageId, duration) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId
                  ? {
                      ...m,
                      thinkingDuration: (m.thinkingDuration ?? 0) + duration,
                      isThinking: false,
                    }
                  : m,
              ),
            }
          : c,
      ),
    }));
  },

  setGenerationDuration: (conversationId, messageId, duration) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId ? { ...m, generationDuration: duration } : m,
              ),
            }
          : c,
      ),
    }));
  },

  addToolCall: (conversationId, messageId, toolCall) => {
    const tcId = genToolId();
    const tc: ToolCallResult = { ...toolCall, id: tcId, startedAt: Date.now() };
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId
                  ? { ...m, toolCalls: [...m.toolCalls, tc] }
                  : m,
              ),
            }
          : c,
      ),
    }));
    return tcId;
  },

  updateToolCall: (conversationId, messageId, toolCallId, updates) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId
                  ? {
                      ...m,
                      toolCalls: m.toolCalls.map((tc) =>
                        tc.id === toolCallId ? { ...tc, ...updates } : tc,
                      ),
                    }
                  : m,
              ),
            }
          : c,
      ),
    }));
  },

  toggleSandbox: (conversationId, messageId) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId ? { ...m, sandboxOpen: !m.sandboxOpen } : m,
              ),
            }
          : c,
      ),
    }));
  },

  openSandbox: (conversationId, messageId) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId ? { ...m, sandboxOpen: true } : m,
              ),
            }
          : c,
      ),
    }));
  },

  setStreaming: (conversationId, messageId, streaming) => {
    set((s) => ({
      isStreaming: streaming,
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId
                  ? {
                      ...m,
                      isStreaming: streaming,
                      ...(streaming ? {} : { completedAt: Date.now() }),
                    }
                  : m,
              ),
            }
          : c,
      ),
    }));
  },

  setModels: (models) => set({ models }),
  setProfileName: (name) => set({ profileName: name }),
  setProfileLabel: (label) => set({ profileLabel: label }),
  setSelectedModel: (model) => set({ selectedModel: model }),

  addSources: (conversationId, messageId, sources) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === messageId
                  ? {
                      ...m,
                      sources: [...(m.sources ?? []), ...sources],
                    }
                  : m,
              ),
            }
          : c,
      ),
    }));
  },

  /**
   * Append a digestion progress event to the user's message. Called
   * from stream.ts when a `document_digest_progress` SSE event arrives.
   * The MessageBubble renders these as an inline progress indicator
   * so the user sees real-time feedback in the conversation itself.
   */
  addDigestProgress: (conversationId, messageId, item) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) => {
                if (m.id !== messageId) return m;
                // Replace any existing item with the same filename+stage
                // to avoid duplicates, then append the new one.
                const existing = (m.digestProgress ?? []).filter(
                  (p) =>
                    !(
                      p.filename === item.filename &&
                      p.stage === item.stage &&
                      p.percent === item.percent
                    ),
                );
                return {
                  ...m,
                  digestProgress: [...existing, item],
                };
              }),
            }
          : c,
      ),
    }));
  },

  // Module actions
  loadModules: async () => {
    set({ isLoadingModules: true });
    try {
      const modules = await apiFetchModules();
      set({ modules, isLoadingModules: false });
    } catch {
      set({ isLoadingModules: false });
    }
  },
  toggleModule: async (moduleName: string, enabled: boolean) => {
    const result = await apiToggleModule(moduleName, enabled);
    if (result) {
      // Update the module in the local state
      set((s) => ({
        modules: s.modules.map((m) =>
          m.name === moduleName
            ? {
                ...m,
                enabled: result.enabled,
                models_downloaded: result.models_downloaded,
              }
            : m,
        ),
      }));
      // Also reload models to include/exclude module models
      const { fetchModels: fm } = await import("@/api/client");
      const { models } = await fm();
      set({ models });
      return true;
    }
    return false;
  },
  setModules: (modules) => set({ modules }),

  getActiveConversation: () => {
    const s = get();
    return s.conversations.find((c) => c.id === s.activeConversationId);
  },

  getActiveMessages: () => {
    const conv = get().getActiveConversation();
    return conv?.messages ?? [];
  },
}));
