import { create } from "zustand";

import { fetchTools, updateTool, type ToolInfo } from "@/api/toolsClient";
import { dbgError } from "@/lib/debug";

// ─── Store ───────────────────────────────────────────────────────

interface ToolsState {
  tools: ToolInfo[];
  isLoading: boolean;
  // tool name → user-facing error message (null = ok)
  saveErrors: Record<string, string | null>;
  // tool name → true while a PUT is in flight
  saving: Record<string, boolean>;

  load: () => Promise<void>;
  /** Patch one tool's config (universal keys and/or custom object). */
  updateConfig: (
    toolName: string,
    config: Record<string, unknown>,
  ) => Promise<boolean>;
  /** Set/clear one secret field (value null → clear). */
  updateSecret: (
    toolName: string,
    field: string,
    value: string | null,
  ) => Promise<boolean>;
  /** Replace a tool row wholesale (after a successful update). */
  applyTool: (tool: ToolInfo) => void;
}

export const useToolsStore = create<ToolsState>((set, get) => ({
  tools: [],
  isLoading: false,
  saveErrors: {},
  saving: {},

  load: async () => {
    set({ isLoading: true });
    const tools = await fetchTools();
    set({ tools, isLoading: false });
  },

  updateConfig: async (toolName, config) => {
    set((s) => ({
      saving: { ...s.saving, [toolName]: true },
      saveErrors: { ...s.saveErrors, [toolName]: null },
    }));
    const { tool, error } = await updateTool(toolName, config);
    if (tool) {
      get().applyTool(tool);
      set((s) => ({ saving: { ...s.saving, [toolName]: false } }));
      return true;
    }
    dbgError(`updateConfig failed for ${toolName}: ${error}`);
    set((s) => ({
      saving: { ...s.saving, [toolName]: false },
      saveErrors: { ...s.saveErrors, [toolName]: error || "saveFailed" },
    }));
    return false;
  },

  updateSecret: async (toolName, field, value) => {
    set((s) => ({
      saving: { ...s.saving, [toolName]: true },
      saveErrors: { ...s.saveErrors, [toolName]: null },
    }));
    const { tool, error } = await updateTool(toolName, {}, { [field]: value });
    if (tool) {
      get().applyTool(tool);
      set((s) => ({ saving: { ...s.saving, [toolName]: false } }));
      return true;
    }
    dbgError(`updateSecret failed for ${toolName}.${field}: ${error}`);
    set((s) => ({
      saving: { ...s.saving, [toolName]: false },
      saveErrors: { ...s.saveErrors, [toolName]: error || "saveFailed" },
    }));
    return false;
  },

  applyTool: (tool) => {
    set((s) => ({
      tools: s.tools.map((t) => (t.tool === tool.tool ? tool : t)),
    }));
  },
}));
