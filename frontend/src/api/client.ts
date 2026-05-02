import type { ModelOption } from "@/store/chatStore";

// ─── Models ──────────────────────────────────────────────────────

export async function fetchModels(): Promise<{
  models: ModelOption[];
  profile: string;
}> {
  try {
    const res = await fetch("/api/profile/models");
    if (res.ok) return await res.json();
  } catch {
    /* fallback below */
  }
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
}

export async function fetchConversations(
  limit = 50,
  offset = 0,
): Promise<ConversationDTO[]> {
  try {
    const res = await fetch(
      `/api/conversations?limit=${limit}&offset=${offset}`,
    );
    if (res.ok) {
      const data = await res.json();
      return data.conversations ?? [];
    }
  } catch {
    /* ignore */
  }
  return [];
}

export async function createConversation(
  title = "New Chat",
  model?: string,
): Promise<ConversationDTO | null> {
  try {
    const params = new URLSearchParams({ title });
    if (model) params.set("model", model);
    const res = await fetch(`/api/conversations?${params.toString()}`, {
      method: "POST",
    });
    if (res.ok) return await res.json();
  } catch {
    /* ignore */
  }
  return null;
}

export async function fetchConversationMessages(
  conversationId: string,
): Promise<MessageDTO[]> {
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
