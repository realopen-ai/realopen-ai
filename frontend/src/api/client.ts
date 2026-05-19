import type { ModelOption } from "@/store/chatStore";
import { dbgError, createDebugLogger } from "@/lib/debug";

const log = createDebugLogger("client");

// ─── Models ──────────────────────────────────────────────────────

export async function fetchModels(): Promise<{
  models: ModelOption[];
  profile: string;
}> {
  log("➡️  fetchModels  url=/api/profile/models");
  try {
    const res = await fetch("/api/profile/models");
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) return await res.json();
    dbgError(`   ❌ fetchModels NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchModels error: ${err}`);
  }
  log("   ⚠️  using fallback models");
  return {
    profile: "16gb",
    models: [
      {
        id: "qwen3:8b",
        type: "chat",
        role: "default",
        description: "Qwen3 8B (Default)",
        size: "5.2 GB",
      },
      {
        id: "moondream:1.8b",
        type: "vision",
        role: "default_vision",
        description: "Moondream 1.8B (Vision)",
        size: "1.7 GB",
      },
      {
        id: "nomic-embed-text:v1.5",
        type: "embedding",
        role: "default_embedding",
        description: "Nomic Embed Text (Embedding)",
        size: "274 MB",
      },
    ],
  };
}

// ─── Conversations ───────────────────────────────────────────────

/** Shape of a tool call as stored in the backend DB (JSON-serialized) */
export interface BackendToolCall {
  id?: string;
  type?: string;
  status?: string;
  title?: string;
  query?: string;
  language?: string;
  code?: string;
  output?: string;
  exitCode?: number;
  results?: { title: string; url: string; snippet: string }[];
  image_description?: string;
  error?: string;
  completedAt?: number;
}

export interface ConversationDTO {
  id: string;
  title: string;
  model: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface MessageDTO {
  id: string;
  conversationId: string;
  role: "user" | "assistant" | "system";
  content: string;
  model: string | null;
  tokens: number | null;
  hasImage: boolean;
  hasDocument: boolean;
  imageCount: number;
  documentCount: number;
  createdAt: number;
  thinking?: string;
  thinkingDuration?: number;
  generationDuration?: number;
  toolCalls?: BackendToolCall[];
}

export async function fetchConversations(
  limit = 50,
  offset = 0,
): Promise<ConversationDTO[]> {
  log(`➡️  fetchConversations  limit=${limit}  offset=${offset}`);
  try {
    const res = await fetch(
      `/api/conversations?limit=${limit}&offset=${offset}`,
    );
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) {
      const data = await res.json();
      return data.conversations ?? [];
    }
  } catch (err) {
    dbgError(`   ❌ fetchConversations error: ${err}`);
  }
  return [];
}

export async function createConversation(
  title = "New Chat",
  model?: string,
): Promise<ConversationDTO | null> {
  log(`➡️  createConversation  title=${title}  model=${model}`);
  try {
    const params = new URLSearchParams({ title });
    if (model) params.set("model", model);
    const res = await fetch(`/api/conversations?${params.toString()}`, {
      method: "POST",
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) return await res.json();
  } catch (err) {
    dbgError(`   ❌ createConversation error: ${err}`);
  }
  return null;
}

export async function fetchConversationMessages(
  conversationId: string,
): Promise<MessageDTO[]> {
  log(`➡️  fetchConversationMessages  convId=${conversationId}`);
  try {
    const res = await fetch(`/api/conversations/${conversationId}`);
    if (res.ok) {
      const data = await res.json();
      return data.messages ?? [];
    }
  } catch {
    /* ignore */
  }
  return [];
}

export async function deleteConversation(
  conversationId: string,
): Promise<boolean> {
  try {
    const res = await fetch(`/api/conversations/${conversationId}`, {
      method: "DELETE",
    });
    return res.ok;
  } catch {
    return false;
  }
}

export async function updateConversationTitle(
  conversationId: string,
  title: string,
): Promise<boolean> {
  try {
    const res = await fetch(
      `/api/conversations/${conversationId}?title=${encodeURIComponent(title)}`,
      { method: "PATCH" },
    );
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * Fetch a conversation's full detail (metadata + messages) by ID.
 * Returns null if the conversation doesn't exist (404) or on error.
 */
export async function fetchConversationDetail(
  conversationId: string,
): Promise<{ conversation: ConversationDTO; messages: MessageDTO[] } | null> {
  log(`➡️  fetchConversationDetail  convId=${conversationId}`);
  try {
    const res = await fetch(`/api/conversations/${conversationId}`);
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) {
      const data = await res.json();
      return {
        conversation: data.conversation,
        messages: data.messages ?? [],
      };
    }
    if (res.status === 404) {
      log("   conversation not found (404)");
      return null;
    }
  } catch (err) {
    dbgError(`   ❌ fetchConversationDetail error: ${err}`);
  }
  return null;
}
