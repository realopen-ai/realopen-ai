import { create } from "zustand";

// ─── Types ───────────────────────────────────────────────────────

export interface ToolCallResult {
  id: string;
  type: "websearch" | "deepsearch" | "code_exec" | "file_read" | "file_write";
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
  // Deep search
  steps?: { label: string; status: "pending" | "running" | "done" }[];
  // Files
  filePath?: string;
  fileContent?: string;
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

  // Actions
  createConversation: () => string;
  deleteConversation: (id: string) => void;
  setActiveConversation: (id: string) => void;
  addMessage: (
    conversationId: string,
    message: Omit<
      Message,
      "id" | "createdAt" | "toolCalls" | "sandboxOpen" | "isStreaming"
    >,
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

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [],
  activeConversationId: null,
  models: [],
  selectedModel: "default",
  profileName: "",
  isStreaming: false,

  createConversation: () => {
    const id = `conv-${Date.now()}`;
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

  deleteConversation: (id) => {
    set((s) => ({
      conversations: s.conversations.filter((c) => c.id !== id),
      activeConversationId:
        s.activeConversationId === id
          ? (s.conversations.find((c) => c.id !== id)?.id ?? null)
          : s.activeConversationId,
    }));
  },

  setActiveConversation: (id) => set({ activeConversationId: id }),

  addMessage: (conversationId, message) => {
    const msgId = genId();
    const msg: Message = {
      ...message,
      id: msgId,
      toolCalls: [],
      sandboxOpen: false,
      isStreaming: false,
      createdAt: Date.now(),
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
                m.id === messageId ? { ...m, isStreaming: streaming } : m,
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
