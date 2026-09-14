/**
 * Tools API client (Brain ▸ Tools).
 *
 * Talks to the backend tool-configuration endpoints:
 *   GET  /api/tools            — every discovered tool + config
 *   GET  /api/tools/{name}     — one tool
 *   PUT  /api/tools/{name}     — update config (+ secrets)
 *
 * The tool list is fully dynamic — nothing is hardcoded here. Custom
 * settings arrive as a `custom_schema` (field descriptors) that a
 * generic renderer can walk; dedicated React components exist for
 * tools that deserve richer UI (see components/brain/tools/).
 */

import { dbgError, createDebugLogger } from "@/lib/debug";

const log = createDebugLogger("toolsClient");

// ─── Types ──────────────────────────────────────────────────────────

/** One custom settings field descriptor from the backend schema. */
export interface ToolConfigField {
  key: string;
  label: string;
  type: "bool" | "string" | "int" | "secret" | "select";
  help?: string;
  placeholder?: string;
  options?: { value: string; label: string }[];
  default?: unknown;
}

/** A section of custom fields (flat for now; keys may be dotted). */
export interface ToolConfigSection {
  key: string;
  label: string;
  fields: ToolConfigField[];
}

/** Masked status of one secret (never the plaintext value). */
export interface SecretStatus {
  set: boolean;
  masked?: string;
}

/** The universal + custom configuration of a tool. */
export interface ToolConfig {
  enabled: boolean;
  always_load: boolean;
  /** Activation tags — when Always load is off, the tool is offered
   * only if the conversation mentions one of these tags. */
  tags: string[];
  /** null → inherit the task-slot / general model */
  model: string | null;
  custom: Record<string, unknown>;
}

/** One tool row from GET /api/tools. */
export interface ToolInfo {
  tool: string;
  display_name: string;
  description: string;
  tool_type: string;
  config: ToolConfig;
  /** Default activation tags (from the tool-loading policy) — used to
   * re-arm the gate when Always load is turned on with no tags. */
  default_tags?: string[];
  custom_schema: ToolConfigSection[] | null;
  has_custom: boolean;
  effective_model: string | null;
  secrets: Record<string, SecretStatus>;
}

// ─── API calls ──────────────────────────────────────────────────────

export async function fetchTools(): Promise<ToolInfo[]> {
  log("➡️  fetchTools  url=/api/tools");
  try {
    const res = await fetch("/api/tools");
    if (res.ok) {
      const data = await res.json();
      return data.tools ?? [];
    }
    dbgError(`   ❌ fetchTools NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchTools error: ${err}`);
  }
  return [];
}

export async function fetchTool(toolName: string): Promise<ToolInfo | null> {
  log(`➡️  fetchTool  tool=${toolName}`);
  try {
    const res = await fetch(`/api/tools/${encodeURIComponent(toolName)}`);
    if (res.ok) return await res.json();
    dbgError(`   ❌ fetchTool NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchTool error: ${err}`);
  }
  return null;
}

/**
 * Update one tool's configuration (partial) and/or secrets.
 *
 * `secrets` maps field → new value (null clears). Secret values are
 * validated server-side where possible and never returned.
 * Returns the updated tool view, or null + a user-facing error message.
 */
export async function updateTool(
  toolName: string,
  config: Record<string, unknown>,
  secrets?: Record<string, string | null>,
): Promise<{ tool?: ToolInfo; error?: string }> {
  log(`➡️  updateTool  tool=${toolName}`);
  try {
    const body: Record<string, unknown> = { config };
    if (secrets) body.secrets = secrets;
    const res = await fetch(`/api/tools/${encodeURIComponent(toolName)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      const tool = await res.json();
      return { tool };
    }
    const err = await res.json().catch(() => ({}));
    return { error: err.detail || `Request failed (${res.status})` };
  } catch (err) {
    dbgError(`   ❌ updateTool error: ${err}`);
    return { error: "Cannot reach the server" };
  }
}
