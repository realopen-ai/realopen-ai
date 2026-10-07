import type { ModelOption } from "@/store/chatStore";
import type { RetrievedSourceDTO } from "@/api/documentsClient";
import { dbgError, createDebugLogger } from "@/lib/debug";

const log = createDebugLogger("client");

// ─── Models ──────────────────────────────────────────────────────

export async function fetchModels(): Promise<{
  models: ModelOption[];
  profile: string;
  label: string;
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
    profile: "cpu_small",
    label: "CPU · Small (≤8 GB RAM)",
    models: [
      {
        id: "qwen3:4b",
        type: "chat",
        role: "default",
        description: "Qwen3 4B (Default)",
        size: "2.5 GB",
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

// ─── AI settings: models + providers ─────────────────────────────

/** A model entry in the Settings ▸ AI ▸ Models dropdowns. */
export interface AvailableModel {
  id: string;
  provider: "ollama" | "groq";
  installed: boolean;
  description: string;
  size: string;
  type: string;
}

/** One task slot row (Chat / Agent, Vision, …). */
export interface ModelTaskSlot {
  task: string;
  label: string;
  model: string;
  provider: "ollama" | "groq";
  default_model: string;
  is_default: boolean;
  local_only: boolean;
}

export interface AvailableModelsResponse {
  models: AvailableModel[];
  tasks: ModelTaskSlot[];
  groq_connected: boolean;
  ollama_connected: boolean;
}

export async function fetchAvailableModels(): Promise<AvailableModelsResponse | null> {
  log("➡️  fetchAvailableModels  url=/api/models/available");
  try {
    const res = await fetch("/api/models/available");
    if (res.ok) return await res.json();
    dbgError(`   ❌ fetchAvailableModels NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchAvailableModels error: ${err}`);
  }
  return null;
}

export async function setModelPreference(
  task: string,
  model: string | null,
): Promise<ModelTaskSlot | null> {
  log(`➡️  setModelPreference  task=${task}  model=${model}`);
  try {
    const res = await fetch("/api/models/preferences", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task, model }),
    });
    if (res.ok) return await res.json();
    const err = await res.json().catch(() => ({}));
    dbgError(`   ❌ setModelPreference error: ${err.detail || res.status}`);
  } catch (err) {
    dbgError(`   ❌ setModelPreference error: ${err}`);
  }
  return null;
}

export interface VoicePersona {
  id: string;
  name: string;
  prompt: string;
}
export interface VoiceChoice {
  id: string;
  name: string;
  voice: string;
}
export interface VoiceSettings {
  voice: string;
  speed: number;
  persona: string;
  custom_personas: VoicePersona[];
  personas: Record<string, string>;
  model: ModelTaskSlot;
}

export async function fetchVoiceSettings(): Promise<VoiceSettings> {
  const res = await fetch("/api/voice/settings");
  if (!res.ok) throw new Error("Could not load voice settings");
  return res.json();
}

export async function updateVoiceSettings(
  patch: Partial<
    Pick<VoiceSettings, "voice" | "speed" | "persona" | "custom_personas">
  >,
): Promise<void> {
  const res = await fetch("/api/voice/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error("Could not save voice settings");
}

export async function fetchVoices(): Promise<{
  builtin: string[];
  custom: VoiceChoice[];
}> {
  const res = await fetch("/api/voice/voices");
  if (!res.ok) throw new Error("Could not load voices");
  return res.json();
}

export async function uploadVoice(file: File): Promise<VoiceChoice> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/voice/voices", { method: "POST", body: form });
  if (!res.ok)
    throw new Error(
      (await res.json().catch(() => ({}))).detail || "Voice import failed",
    );
  return res.json();
}

// ─── Providers ─────────────────────────────────────────────────────

export interface ProviderInfo {
  id: string;
  name: string;
  kind: "local" | "cloud";
  connected: boolean;
  model_count: number;
  has_key?: boolean;
  key_masked?: string;
}

export async function fetchProviders(): Promise<ProviderInfo[]> {
  log("➡️  fetchProviders  url=/api/providers");
  try {
    const res = await fetch("/api/providers");
    if (res.ok) {
      const data = await res.json();
      return data.providers ?? [];
    }
    dbgError(`   ❌ fetchProviders NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchProviders error: ${err}`);
  }
  return [];
}

/**
 * Connect the Groq provider: the backend validates the key with a mini
 * request to Groq and saves it only when Groq accepts it.
 * Returns { ok, error } — error carries the user-facing message.
 */
export async function connectGroqProvider(
  apiKey: string,
): Promise<{ ok: boolean; error?: string; keyMasked?: string }> {
  log("➡️  connectGroqProvider");
  try {
    const res = await fetch("/api/providers/groq", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
    });
    if (res.ok) {
      const data = await res.json();
      return { ok: true, keyMasked: data.key_masked };
    }
    const err = await res.json().catch(() => ({}));
    return { ok: false, error: err.detail || `Request failed (${res.status})` };
  } catch (err) {
    dbgError(`   ❌ connectGroqProvider error: ${err}`);
    return { ok: false, error: "Cannot reach the server" };
  }
}

export async function disconnectGroqProvider(): Promise<boolean> {
  log("➡️  disconnectGroqProvider");
  try {
    const res = await fetch("/api/providers/groq", { method: "DELETE" });
    return res.ok;
  } catch (err) {
    dbgError(`   ❌ disconnectGroqProvider error: ${err}`);
    return false;
  }
}

// ─── Modules ─────────────────────────────────────────────────────

export interface ModuleInfo {
  name: string;
  required: boolean;
  enabled: boolean;
  label: string;
  description: string;
  icon: string;
  available: boolean;
  models_downloaded: boolean;
  can_toggle: boolean;
  requirements_met: boolean;
  minimum_requirements?: { ram: number; vram: number };
  estimated_size?: string;
  models?: ModelOption[];
  model_roles?: string[];
}

export async function fetchModules(): Promise<ModuleInfo[]> {
  log("➡️  fetchModules  url=/api/modules");
  try {
    const res = await fetch("/api/modules");
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) {
      const data = await res.json();
      return data.modules ?? [];
    }
    dbgError(`   ❌ fetchModules NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchModules error: ${err}`);
  }
  return [];
}

export async function toggleModule(
  moduleName: string,
  enabled: boolean,
): Promise<{
  module: string;
  enabled: boolean;
  models_downloaded: boolean;
} | null> {
  log(`➡️  toggleModule  module=${moduleName}  enabled=${enabled}`);
  try {
    const res = await fetch("/api/modules/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ module: moduleName, enabled }),
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) return await res.json();
    const err = await res.json().catch(() => ({}));
    dbgError(`   ❌ toggleModule error: ${err.detail || res.status}`);
  } catch (err) {
    dbgError(`   ❌ toggleModule error: ${err}`);
  }
  return null;
}

export async function installModuleModels(
  moduleName: string,
  onEvent: (event: Record<string, unknown>) => void,
): Promise<boolean> {
  log(`➡️  installModuleModels  module=${moduleName}`);
  try {
    const res = await fetch("/api/modules/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ module: moduleName }),
    });

    if (!res.ok || !res.body) {
      dbgError(`   ❌ installModuleModels NOT OK  status=${res.status}`);
      return false;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try {
          const event = JSON.parse(line.slice(6));
          onEvent(event);
        } catch {
          // Skip malformed JSON
        }
      }
    }

    return true;
  } catch (err) {
    dbgError(`   ❌ installModuleModels error: ${err}`);
    return false;
  }
}

// ─── Conversations ───────────────────────────────────────────────

/** Shape of a tool call as stored inside a block's `tool_call` field */
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
  webResults?: { title: string; url: string; snippet: string }[];
  genResults?: {
    type: string;
    data?: string;
    filename?: string;
    format?: string;
    download_url?: string;
    report_id?: string;
    file_path?: string;
    created_at?: number;
    deck_id?: string;
    note_id?: string;
    title?: string;
    card_count?: number;
  }[];
  imageDescription?: string;
  filePath?: string;
  fileContent?: string;
  diff?: string;
  previewUrl?: string;
  previewPort?: number;
  sandboxId?: string;
  sandbox?: {
    id: string;
    name: string;
    status: string;
    desired_running: boolean;
    cpu_limit: number;
    memory_limit_mb: number;
    workspace_quota_bytes: number;
    usage_bytes: number;
    idle_timeout_seconds: number;
    error?: string | null;
  };
  error?: string;
  startedAt?: number;
  completedAt?: number;
  durationMs?: number;
  sources?: RetrievedSourceDTO[];
}

/** A rendering block as stored in the messages.blocks JSONB column. */
export interface BackendBlock {
  type: "thinking" | "text" | "tool_call" | "error";
  content?: string;
  duration?: number | null;
  tool_call?: BackendToolCall;
}

export interface ConversationDTO {
  id: string;
  title: string;
  model: string | null;
  sandboxId?: string | null;
  createdAt: number;
  updatedAt: number;
  // Sidebar organization flags (three-dots menu).
  pinned?: boolean;
  archived?: boolean;
  pinnedAt?: number | null;
  archivedAt?: number | null;
  // Cross-session context visibility — lets the Brain page show which
  // conversations have been summarized and what the summary is.
  summary?: string | null;
  summaryAt?: number | null;
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
  /** How the message was produced: spoken ("voice") or typed ("text").
   * Null for messages created before the voice feature. */
  modality?: "voice" | "text" | null;
  /** Ordered rendering blocks (thinking/text/tool_call/error) for
   * assistant messages. NULL for user/system messages. */
  blocks?: BackendBlock[] | null;
  generationDuration?: number;
  completionStatus?: "streaming" | "completed" | "interrupted" | "error";
  /** Deliverable files (reports, etc.) produced by tool calls. NULL
   * when no deliverables. The frontend renders download badges from
   * this so they survive page refresh. */
  deliverables?: BackendDeliverable[] | null;
}

/** A deliverable file as stored in the messages.deliverables JSONB column. */
export interface BackendDeliverable {
  type: string; // "report"
  format: string; // "pdf" | "docx"
  filename: string;
  file_path: string;
  download_url: string;
  report_id?: string;
  thumbnail_url?: string;
  created_at?: number;
}

/**
 * Fetch the conversation list.
 *
 * `archived` filters the result: omitted = all conversations,
 * false = only active (sidebar main list), true = only archived
 * (sidebar "Archived" section). The backend orders pinned conversations
 * first, then most-recently-updated.
 */
export async function fetchConversations(
  limit = 50,
  offset = 0,
  archived?: boolean,
): Promise<ConversationDTO[]> {
  log(
    `➡️  fetchConversations  limit=${limit}  offset=${offset}  archived=${archived}`,
  );
  try {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });
    if (archived !== undefined) {
      params.set("archived", String(archived));
    }
    const res = await fetch(`/api/conversations?${params.toString()}`);
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
 * Update a conversation's pinned / archived flags (three-dots menu).
 * Only the flags present in `flags` are sent; the backend leaves the
 * others unchanged. Returns true when the request succeeded.
 */
export async function updateConversationFlags(
  conversationId: string,
  flags: { pinned?: boolean; archived?: boolean },
): Promise<boolean> {
  log(
    `➡️  updateConversationFlags  convId=${conversationId}  flags=${JSON.stringify(flags)}`,
  );
  try {
    const params = new URLSearchParams();
    if (flags.pinned !== undefined) {
      params.set("pinned", String(flags.pinned));
    }
    if (flags.archived !== undefined) {
      params.set("archived", String(flags.archived));
    }
    const res = await fetch(
      `/api/conversations/${conversationId}?${params.toString()}`,
      { method: "PATCH" },
    );
    log(`   response  status=${res.status}  ok=${res.ok}`);
    return res.ok;
  } catch (err) {
    dbgError(`   ❌ updateConversationFlags error: ${err}`);
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

// ─── Past-conversation search ────────────────────────────────────

export interface PastConversationResult {
  message_id: string;
  conversation_id: string;
  conversation_title: string;
  role: string;
  content_snippet: string;
  content_full: string;
  rank: number;
  created_at: number;
}

export async function searchPastConversations(
  query: string,
  excludeConversationId?: string,
  limit = 10,
): Promise<PastConversationResult[]> {
  log(`➡️  searchPastConversations  query=${query}`);
  try {
    const params = new URLSearchParams({ query, limit: String(limit) });
    if (excludeConversationId) {
      params.set("exclude_conversation_id", excludeConversationId);
    }
    const res = await fetch(`/api/conversations/search?${params.toString()}`, {
      method: "POST",
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) {
      const data = await res.json();
      return data.results ?? [];
    }
    dbgError(`   ❌ searchPastConversations NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ searchPastConversations error: ${err}`);
  }
  return [];
}
