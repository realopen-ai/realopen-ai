import { create } from "zustand";
import {
  fetchConversations,
  createConversation as apiCreateConversation,
  deleteConversation as apiDeleteConversation,
  fetchConversationMessages,
  fetchConversationDetail,
  type ConversationDTO,
  type MessageDTO,
} from "@/api/client";

// ─── Types ───────────────────────────────────────────────────────

export interface ToolCallResult {
  id: string;
  type:
    | "websearch"
    | "vision"
    | "code_exec"
    | "file_read"
    | "file_write"
    | "deepsearch";
  status: "running" | "completed" | "error";
  title: string;
  startedAt: number;
  completedAt?: number;
  // Web search
  query?: string;
  results?: { title: string; url: string; snippet: string }[];
  // Code exec
  language?: string;
  code?: string;
  output?: string;
  exitCode?: number;
  // Vision
  imageDescription?: string;
  // Deep search
  steps?: { label: string; status: "pending" | "running" | "done" }[];
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
  isStreaming: boolean;
  isLoadingConversations: boolean;

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
  setSelectedModel: (model: string) => void;

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
    results: tc.results,
    language: tc.language,
    code: tc.code,
    output: tc.output,
    exitCode: tc.exitCode,
    imageDescription: tc.image_description,
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
  isStreaming: false,
  isLoadingConversations: false,

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
  setSelectedModel: (model) => set({ selectedModel: model }),

  getActiveConversation: () => {
    const s = get();
    return s.conversations.find((c) => c.id === s.activeConversationId);
  },

  getActiveMessages: () => {
    const conv = get().getActiveConversation();
    return conv?.messages ?? [];
  },
}));
