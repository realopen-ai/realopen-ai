import { create } from "zustand";
import {
  fetchConversations,
  createConversation as apiCreateConversation,
  deleteConversation as apiDeleteConversation,
  updateConversationTitle as apiUpdateConversationTitle,
  updateConversationFlags as apiUpdateConversationFlags,
  fetchConversationMessages,
  fetchConversationDetail,
  fetchModules as apiFetchModules,
  toggleModule as apiToggleModule,
  type ConversationDTO,
  type MessageDTO,
  type ModuleInfo,
} from "@/api/client";
import type { RetrievedSourceDTO } from "@/api/documentsClient";
import { persistedToolCallDetails } from "@/store/toolCallPersistence";
import type { Sandbox } from "@/store/sandboxStore";
import { epochMilliseconds } from "@/lib/timing";

// ─── Types ───────────────────────────────────────────────────────

export interface ToolCallResult {
  id: string;
  type:
    | "websearch"
    | "vision"
    | "code_exec"
    | "skill"
    | "flashcards"
    | "notes"
    | "artifact"
    | "file_read"
    | "file_write"
    | "deepsearch"
    | "image_gen"
    | "sandbox"
    | "preview";
  status: "running" | "completed" | "error";
  title: string;
  startedAt: number;
  completedAt?: number;
  durationMs?: number;
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
  progress?: { stage: string; completed: number; total: number };
  // Media generation — images (type: "image", data: base64) and reports
  // (type: "report", format, filename, download_url, etc.)
  genResults?: {
    type: string;
    data?: string;
    filename?: string;
    format?: string;
    download_url?: string;
    thumbnail_url?: string;
    report_id?: string;
    file_path?: string;
    created_at?: number;
    deck_id?: string;
    note_id?: string;
    artifact_id?: string;
    version?: number;
    title?: string;
    card_count?: number;
  }[];
  // Files
  filePath?: string;
  fileContent?: string;
  diff?: string;
  previewUrl?: string;
  previewPort?: number;
  refreshFiles?: boolean;
  sandboxId?: string;
  sandbox?: Sandbox;
  // Error
  error?: string;
  // RAG sources (for rag_search tool calls)
  sources?: RetrievedSourceDTO[];
}

/** A single rendering block in a multi-round assistant message.
 * Blocks are rendered in array order to preserve the chronological flow
 * of the agent turn (thinking → text → tool_call → thinking → ...).
 */
export interface MessageBlock {
  /** Frontend-generated stable ID for React keys. NOT persisted to DB
   * (the DB uses array index as implicit order). */
  id: string;
  type: "thinking" | "text" | "tool_call" | "error";
  // thinking / text / error
  content?: string;
  // thinking only — seconds, null while still thinking
  duration?: number | null;
  // tool_call only
  toolCall?: ToolCallResult;
}

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  model?: string;
  modality?: "voice" | "text";
  /** Ordered rendering blocks for assistant messages. NULL/undefined for
   * user/system messages. Each block is rendered in order to show the
   * chronological flow of a multi-round agent turn. */
  blocks?: MessageBlock[];
  isStreaming: boolean;
  createdAt: number;
  completedAt?: number;
  shrugOverlay?: boolean;
  // Image / document info
  hasImage?: boolean;
  hasDocument?: boolean;
  imageCount?: number;
  documentCount?: number;
  // Total generation duration across all agent rounds (seconds).
  generationDuration?: number;
  completionStatus?: "streaming" | "completed" | "interrupted" | "error";
  // Deliverable files (reports, etc.) produced by tool calls. Persisted
  // in the DB so download badges survive page refresh.
  deliverables?: Deliverable[];
  // Document digestion progress — populated when the user uploads docs
  // via chat and the backend streams document_digest_* SSE events.
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

/** A deliverable file (report, etc.) produced by a tool call. Persisted
 * in the DB so download badges survive page refresh. */
export interface Deliverable {
  type: string; // "report" | "presentation" | "excel"
  format: string; // "pdf" | "docx" | "pptx" | "xlsx"
  filename: string;
  file_path: string;
  download_url: string;
  thumbnail_url?: string;
  report_id?: string;
  created_at?: number;
}

export interface Conversation {
  id: string;
  title: string;
  messages: Message[];
  model: string;
  sandboxId?: string | null;
  createdAt: number;
  updatedAt: number;
  pinned: boolean;
  archived: boolean;
  pinnedAt: number | null;
  archivedAt: number | null;
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
    message: Omit<Message, "id" | "createdAt" | "isStreaming"> & {
      shrugOverlay?: boolean;
    },
  ) => string;
  updateMessage: (
    conversationId: string,
    messageId: string,
    updates: Partial<Message>,
  ) => void;
  setStreaming: (
    conversationId: string,
    messageId: string,
    streaming: boolean,
  ) => void;
  renameConversation: (id: string, title: string) => Promise<void>;
  togglePinConversation: (id: string) => Promise<void>;
  toggleArchiveConversation: (id: string) => Promise<void>;
  setConversationTitle: (conversationId: string, title: string) => void;
  setModels: (models: ModelOption[]) => void;
  setProfileName: (name: string) => void;
  setProfileLabel: (label: string) => void;
  setSelectedModel: (model: string) => void;

  // ── Block-based actions (replace the old toolCalls/thinking actions) ──
  /** Start a new thinking block. Closes any open text block. */
  startThinkingBlock: (conversationId: string, messageId: string) => void;
  /** Append a thinking token to the current thinking block. */
  appendThinkingToken: (
    conversationId: string,
    messageId: string,
    token: string,
  ) => void;
  /** Finalize the current thinking block with a duration. */
  finishThinkingBlock: (
    conversationId: string,
    messageId: string,
    duration: number,
  ) => void;
  /** Append a text token. Opens a new text block if the last block isn't text. */
  appendTextToken: (
    conversationId: string,
    messageId: string,
    token: string,
  ) => void;
  /** Start a new tool_call block with status=running. Returns the block ID
   * (which matches the backend tool call ID for later updates). */
  startToolCallBlock: (
    conversationId: string,
    messageId: string,
    toolCall: ToolCallResult,
  ) => void;
  /** Update a tool_call block by its tool call ID. */
  updateToolCallBlock: (
    conversationId: string,
    messageId: string,
    toolCallId: string,
    updates: Partial<ToolCallResult>,
  ) => void;
  /** Attach RAG sources to a tool_call block by tool call ID. */
  setToolCallSources: (
    conversationId: string,
    messageId: string,
    toolCallId: string,
    sources: RetrievedSourceDTO[],
  ) => void;
  /** Set the total generation duration on the message. */
  setGenerationDuration: (
    conversationId: string,
    messageId: string,
    duration: number,
  ) => void;

  // RAG digestion progress — append a progress item to the user's
  // message when a `document_digest_*` SSE event arrives during chat
  // upload.
  addDigestProgress: (
    conversationId: string,
    messageId: string,
    item: DigestProgressItem,
  ) => void;

  // Deliverables — append deliverable file metadata to a message when
  // a `deliverables` SSE event arrives (report/presentation generated).
  // This makes the download badges show up immediately without refresh.
  addDeliverables: (
    conversationId: string,
    messageId: string,
    deliverables: Deliverable[],
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

/** Convert a backend ConversationDTO to the frontend Conversation shape */
function dtoToConversation(dto: ConversationDTO): Conversation {
  return {
    id: dto.id,
    title: dto.title ?? "New Chat",
    messages: [], // Messages are loaded lazily when a conversation is selected
    model: dto.model ?? "default",
    createdAt: dto.createdAt,
    updatedAt: dto.updatedAt,
    pinned: dto.pinned ?? false,
    archived: dto.archived ?? false,
    pinnedAt: dto.pinnedAt ?? null,
    archivedAt: dto.archivedAt ?? null,
  };
}

/** Convert a backend MessageDTO to the frontend Message shape.
 *
 * The backend stores blocks as a JSONB array; we add frontend-generated
 * IDs for React keys (the DB uses array index as implicit order).
 *
 * Exported so the voice pipeline can convert the `assistant_message`
 * WebSocket event (same DTO message shape) into final Message fields.
 */
export function dtoToMessage(dto: MessageDTO): Message {
  // Convert backend blocks (raw JSON) to frontend MessageBlock[] with IDs.
  const blocks: MessageBlock[] | undefined = dto.blocks
    ? dto.blocks.map((b, i) => {
        if (b.type === "tool_call" && b.tool_call) {
          return {
            id: `block-${dto.id}-${i}`,
            type: "tool_call",
            toolCall: {
              id: b.tool_call.id ?? `tc-${dto.id}-${i}`,
              type: (b.tool_call.type as ToolCallResult["type"]) ?? "websearch",
              status:
                (b.tool_call.status as ToolCallResult["status"]) ?? "completed",
              title: b.tool_call.title ?? b.tool_call.type ?? "Tool",
              startedAt: epochMilliseconds(
                b.tool_call.startedAt,
                dto.createdAt,
              ),
              completedAt:
                b.tool_call.completedAt == null
                  ? undefined
                  : epochMilliseconds(b.tool_call.completedAt),
              durationMs: b.tool_call.durationMs,
              progress: b.tool_call.progress,
              query: b.tool_call.query,
              webResults: b.tool_call.webResults,
              genResults: b.tool_call.genResults,
              language: b.tool_call.language,
              code: b.tool_call.code,
              output: b.tool_call.output,
              exitCode: b.tool_call.exitCode,
              ...persistedToolCallDetails(b.tool_call),
              imageDescription: b.tool_call.imageDescription,
              error: b.tool_call.error,
              sources: b.tool_call.sources,
            },
          };
        }
        return {
          id: `block-${dto.id}-${i}`,
          type: b.type as MessageBlock["type"],
          content: b.content,
          duration: b.duration,
        };
      })
    : undefined;

  return {
    id: dto.id,
    role: dto.role,
    content: dto.content,
    model: dto.model ?? undefined,
    modality: (dto.modality as "voice" | "text" | undefined) ?? undefined,
    blocks,
    isStreaming: dto.completionStatus === "streaming",
    createdAt: dto.createdAt,
    hasImage: dto.hasImage,
    hasDocument: dto.hasDocument,
    imageCount: dto.imageCount,
    documentCount: dto.documentCount,
    generationDuration: dto.generationDuration,
    completionStatus: dto.completionStatus ?? "completed",
    deliverables: (dto.deliverables ?? undefined)?.map((d) => ({
      ...d,
      thumbnail_url:
        d.thumbnail_url ??
        (d.format === "pptx" && d.report_id
          ? `/api/reports/${d.report_id}/thumbnail`
          : undefined),
    })),
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
      // Fetch active + archived conversations in parallel (the sidebar
      // shows active ones in the main list and archived ones under a
      // collapsible "Archived" section).
      const [activeDtos, archivedDtos] = await Promise.all([
        fetchConversations(50, 0, false),
        fetchConversations(100, 0, true),
      ]);
      const dtos = [...activeDtos, ...archivedDtos];
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
      pinned: false,
      archived: false,
      pinnedAt: null,
      archivedAt: null,
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
      blocks: message.blocks ?? (message.role === "assistant" ? [] : undefined),
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

  setConversationTitle: (conversationId, title) => {
    const clean = (title ?? "").trim();
    if (!clean) return;
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId ? { ...c, title: clean } : c,
      ),
    }));
  },

  renameConversation: async (id, title) => {
    const clean = (title ?? "").trim();
    if (!clean) return;
    const conv = get().conversations.find((c) => c.id === id);
    if (!conv || conv.title === clean) return;
    // Optimistic local update
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id ? { ...c, title: clean } : c,
      ),
    }));
    const ok = await apiUpdateConversationTitle(id, clean);
    if (!ok) {
      // Revert on failure
      set((s) => ({
        conversations: s.conversations.map((c) =>
          c.id === id ? { ...c, title: conv.title } : c,
        ),
      }));
    }
  },

  togglePinConversation: async (id) => {
    const conv = get().conversations.find((c) => c.id === id);
    if (!conv) return;
    const next = !conv.pinned;
    const prev = {
      pinned: conv.pinned,
      pinnedAt: conv.pinnedAt,
      archived: conv.archived,
      archivedAt: conv.archivedAt,
    };
    // Optimistic local update (pin implies unarchive — mirrors the backend)
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id
          ? {
              ...c,
              pinned: next,
              pinnedAt: next ? Date.now() : null,
              archived: next ? false : c.archived,
              archivedAt: next ? null : c.archivedAt,
            }
          : c,
      ),
    }));
    const ok = await apiUpdateConversationFlags(id, { pinned: next });
    if (!ok) {
      // Revert on failure
      set((s) => ({
        conversations: s.conversations.map((c) =>
          c.id === id ? { ...c, ...prev } : c,
        ),
      }));
    }
  },

  toggleArchiveConversation: async (id) => {
    const conv = get().conversations.find((c) => c.id === id);
    if (!conv) return;
    const next = !conv.archived;
    const prev = {
      pinned: conv.pinned,
      pinnedAt: conv.pinnedAt,
      archived: conv.archived,
      archivedAt: conv.archivedAt,
    };
    // Optimistic local update (archive implies unpin — mirrors the backend)
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id
          ? {
              ...c,
              archived: next,
              archivedAt: next ? Date.now() : null,
              pinned: next ? false : c.pinned,
              pinnedAt: next ? null : c.pinnedAt,
            }
          : c,
      ),
    }));
    const ok = await apiUpdateConversationFlags(id, { archived: next });
    if (!ok) {
      // Revert on failure
      set((s) => ({
        conversations: s.conversations.map((c) =>
          c.id === id ? { ...c, ...prev } : c,
        ),
      }));
    }
  },

  // ── Block-based actions ───────────────────────────────────────────
  // These reconstruct the ordered blocks array from SSE events. Each
  // action finds the message and mutates its blocks array in place.
  // Block IDs are generated as `block-${msgId}-${counter}` so they're
  // stable across re-renders.

  startThinkingBlock: (conversationId, messageId) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) => {
                if (m.id !== messageId) return m;
                const blocks = m.blocks ?? [];
                const blockId = `block-${messageId}-${blocks.length}`;
                return {
                  ...m,
                  blocks: [
                    ...blocks,
                    {
                      id: blockId,
                      type: "thinking" as const,
                      content: "",
                      duration: null,
                    },
                  ],
                };
              }),
            }
          : c,
      ),
    }));
  },

  appendThinkingToken: (conversationId, messageId, token) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) => {
                if (m.id !== messageId || !m.blocks?.length) return m;
                const blocks = [...m.blocks];
                const last = blocks[blocks.length - 1];
                if (last.type === "thinking") {
                  blocks[blocks.length - 1] = {
                    ...last,
                    content: (last.content ?? "") + token,
                  };
                }
                return { ...m, blocks };
              }),
            }
          : c,
      ),
    }));
  },

  finishThinkingBlock: (conversationId, messageId, duration) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) => {
                if (m.id !== messageId || !m.blocks?.length) return m;
                const blocks = [...m.blocks];
                const last = blocks[blocks.length - 1];
                if (last.type === "thinking") {
                  blocks[blocks.length - 1] = { ...last, duration };
                }
                return { ...m, blocks };
              }),
            }
          : c,
      ),
    }));
  },

  appendTextToken: (conversationId, messageId, token) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) => {
                if (m.id !== messageId) return m;
                const blocks = m.blocks ?? [];
                const last = blocks[blocks.length - 1];
                // If the last block is text, append to it.
                if (last && last.type === "text") {
                  const newBlocks = [...blocks];
                  newBlocks[newBlocks.length - 1] = {
                    ...last,
                    content: (last.content ?? "") + token,
                  };
                  return {
                    ...m,
                    blocks: newBlocks,
                    content: (m.content ?? "") + token,
                  };
                }
                // Otherwise, start a new text block.
                const blockId = `block-${messageId}-${blocks.length}`;
                return {
                  ...m,
                  blocks: [
                    ...blocks,
                    { id: blockId, type: "text" as const, content: token },
                  ],
                  content: (m.content ?? "") + token,
                };
              }),
            }
          : c,
      ),
    }));
  },

  startToolCallBlock: (conversationId, messageId, toolCall) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) => {
                if (m.id !== messageId) return m;
                const blocks = m.blocks ?? [];
                // Buffered SSE can replay a start already present in the DB snapshot.
                if (
                  blocks.some(
                    (b) =>
                      b.type === "tool_call" && b.toolCall?.id === toolCall.id,
                  )
                ) {
                  return m;
                }
                const blockId = `block-${messageId}-${blocks.length}`;
                return {
                  ...m,
                  blocks: [
                    ...blocks,
                    {
                      id: blockId,
                      type: "tool_call" as const,
                      toolCall,
                    },
                  ],
                };
              }),
            }
          : c,
      ),
    }));
  },

  updateToolCallBlock: (conversationId, messageId, toolCallId, updates) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) => {
                if (m.id !== messageId || !m.blocks) return m;
                return {
                  ...m,
                  blocks: m.blocks.map((b) =>
                    b.type === "tool_call" && b.toolCall?.id === toolCallId
                      ? { ...b, toolCall: { ...b.toolCall, ...updates } }
                      : b,
                  ),
                };
              }),
            }
          : c,
      ),
    }));
  },

  setToolCallSources: (conversationId, messageId, toolCallId, sources) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) => {
                if (m.id !== messageId || !m.blocks) return m;
                return {
                  ...m,
                  blocks: m.blocks.map((b) =>
                    b.type === "tool_call" && b.toolCall?.id === toolCallId
                      ? { ...b, toolCall: { ...b.toolCall, sources } }
                      : b,
                  ),
                };
              }),
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
                m.id === messageId
                  ? {
                      ...m,
                      generationDuration:
                        (m.generationDuration ?? 0) + duration,
                    }
                  : m,
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

  addDeliverables: (conversationId, messageId, deliverables) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? {
              ...c,
              messages: c.messages.map((m) => {
                if (m.id !== messageId) return m;
                // Avoid duplicates: only add deliverables whose download_url
                // isn't already in the message's deliverables array.
                const existingUrls = new Set(
                  (m.deliverables ?? []).map((d) => d.download_url),
                );
                const newOnes = deliverables
                  .filter((d) => !existingUrls.has(d.download_url))
                  .map((d) => ({
                    ...d,
                    // Fallback: construct thumbnail_url for PPTX if missing
                    thumbnail_url:
                      d.thumbnail_url ??
                      (d.format === "pptx" && d.report_id
                        ? `/api/reports/${d.report_id}/thumbnail`
                        : undefined),
                  }));
                if (newOnes.length === 0) return m;
                return {
                  ...m,
                  deliverables: [...(m.deliverables ?? []), ...newOnes],
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
