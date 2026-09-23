import { create } from "zustand";

export interface FileNode {
  name: string;
  type: "file" | "directory";
  path: string;
  children?: FileNode[];
  size?: number;
  modifiedAt?: string;
}
const HIDDEN_WORKSPACE_DIRS = new Set([
  ".venv",
  "venv",
  ".pytest_cache",
  "__pycache__",
  "node_modules",
  "dist",
  "build",
]);
export function visibleWorkspaceTree(nodes: FileNode[]): FileNode[] {
  return nodes
    .filter((node) => !HIDDEN_WORKSPACE_DIRS.has(node.name))
    .map((node) => ({
      ...node,
      ...(node.children
        ? { children: visibleWorkspaceTree(node.children) }
        : {}),
    }));
}
export interface Sandbox {
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
}
export interface SandboxCreateOptions {
  name: string;
  conversationId?: string | null;
  cpuLimit: number;
  memoryLimitMb: number;
  workspaceQuotaBytes: number;
}
export interface SandboxCommand {
  id: string;
  source: "general_agent" | "coder_agent";
  tool_name: string;
  command: string;
  stdout: string;
  stderr: string;
  exit_code: number | null;
  output_truncated: boolean;
}

export function commandHistoryLines(commands: SandboxCommand[]): string[] {
  return commands.flatMap((item) => {
    const command =
      item.source === "general_agent"
        ? `$ python\n${item.command
            .split("\n")
            .map((line) => `  ${line}`)
            .join("\n")}`
        : `$ ${item.command}`;
    const lines = [command];
    if (item.stdout) lines.push(item.stdout);
    if (item.stderr)
      lines.push(`Error (exit ${item.exit_code ?? "?"}):\n${item.stderr}`);
    if (item.output_truncated) lines.push("  [output truncated]");
    return lines;
  });
}

interface SandboxState {
  sandboxes: Sandbox[];
  sandboxId: string | null;
  fileTree: FileNode[];
  isLoadingTree: boolean;
  activeFile: string | null;
  activeFileContent: string | null;
  previewUrl: string | null;
  terminalHistory: string[];
  loading: boolean;
  error: string | null;
  setFileTree: (tree: FileNode[]) => void;
  setLoadingTree: (v: boolean) => void;
  setActiveFile: (path: string | null, content?: string | null) => void;
  addTerminalLine: (line: string) => void;
  clearTerminal: () => void;
  setSandboxId: (id: string | null) => void;
  setPreviewUrl: (url: string | null) => void;
  loadCommandHistory: () => Promise<void>;
  activateSandbox: (sandboxId: string) => Promise<void>;
  loadSandboxes: () => Promise<void>;
  loadForConversation: (conversationId: string | null) => Promise<void>;
  createSandbox: (options: SandboxCreateOptions) => Promise<Sandbox>;
  linkSandbox: (sandboxId: string, conversationId: string) => Promise<void>;
  lifecycle: (action: "start" | "stop" | "restart") => Promise<void>;
  deleteSandbox: (id: string) => Promise<void>;
  fetchFileTree: () => Promise<void>;
  fetchFileContent: (path: string) => Promise<void>;
  saveFileContent: (path: string, content: string) => Promise<void>;
}

let conversationLoadRevision = 0;

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || `Request failed (${response.status})`);
  }
  return response.json();
}

export const useSandboxStore = create<SandboxState>((set, get) => ({
  sandboxes: [],
  sandboxId: null,
  fileTree: [],
  isLoadingTree: false,
  activeFile: null,
  activeFileContent: null,
  previewUrl: null,
  terminalHistory: [],
  loading: false,
  error: null,
  setFileTree: (fileTree) => set({ fileTree }),
  setLoadingTree: (isLoadingTree) => set({ isLoadingTree }),
  setActiveFile: (activeFile, content) =>
    set({ activeFile, activeFileContent: content ?? null }),
  addTerminalLine: (line) =>
    set((s) => ({ terminalHistory: [...s.terminalHistory, line] })),
  clearTerminal: () => set({ terminalHistory: [] }),
  setPreviewUrl: (previewUrl) => set({ previewUrl }),
  setSandboxId: (sandboxId) => {
    conversationLoadRevision += 1;
    set({ sandboxId, fileTree: [], activeFile: null, terminalHistory: [] });
  },
  loadCommandHistory: async () => {
    const id = get().sandboxId;
    if (!id) {
      set({ terminalHistory: [] });
      return;
    }
    try {
      const d = await request<{ commands: SandboxCommand[] }>(
        `/api/sandboxes/${id}/commands`,
      );
      set({ terminalHistory: commandHistoryLines(d.commands) });
    } catch (e) {
      set({ error: String(e) });
    }
  },
  activateSandbox: async (sandboxId) => {
    conversationLoadRevision += 1;
    if (get().sandboxId !== sandboxId)
      set({
        sandboxId,
        fileTree: [],
        activeFile: null,
        activeFileContent: null,
        terminalHistory: [],
        error: null,
      });
    await Promise.all([
      get().loadSandboxes(),
      get().fetchFileTree(),
      get().loadCommandHistory(),
    ]);
  },
  loadSandboxes: async () => {
    set({ loading: true, error: null });
    try {
      const d = await request<{ sandboxes: Sandbox[] }>("/api/sandboxes");
      set({ sandboxes: d.sandboxes });
    } catch (e) {
      set({ error: String(e) });
    } finally {
      set({ loading: false });
    }
  },
  loadForConversation: async (conversationId) => {
    const revision = ++conversationLoadRevision;
    if (!conversationId) {
      set({ sandboxId: null, fileTree: [], terminalHistory: [] });
      return;
    }
    try {
      const d = await request<{ sandbox: Sandbox | null }>(
        `/api/sandboxes/conversation/${conversationId}`,
      );
      if (revision !== conversationLoadRevision) return;
      set({ sandboxId: d.sandbox?.id ?? null, terminalHistory: [] });
      if (d.sandbox)
        await Promise.all([get().fetchFileTree(), get().loadCommandHistory()]);
    } catch (e) {
      if (revision === conversationLoadRevision) set({ error: String(e) });
    }
  },
  createSandbox: async ({
    name,
    conversationId,
    cpuLimit,
    memoryLimitMb,
    workspaceQuotaBytes,
  }) => {
    const item = await request<Sandbox>("/api/sandboxes", {
      method: "POST",
      body: JSON.stringify({
        name,
        conversation_id: conversationId || null,
        cpu_limit: cpuLimit,
        memory_limit_mb: memoryLimitMb,
        workspace_quota_bytes: workspaceQuotaBytes,
      }),
    });
    set((s) => ({
      sandboxes: [item, ...s.sandboxes],
      sandboxId: conversationId ? item.id : s.sandboxId,
    }));
    return item;
  },
  linkSandbox: async (sandboxId, conversationId) => {
    await request(`/api/sandboxes/${sandboxId}/link/${conversationId}`, {
      method: "PUT",
    });
    set({ sandboxId, terminalHistory: [] });
    await Promise.all([get().fetchFileTree(), get().loadCommandHistory()]);
  },
  lifecycle: async (action) => {
    const id = get().sandboxId;
    if (!id) return;
    const item = await request<Sandbox>(`/api/sandboxes/${id}/${action}`, {
      method: "POST",
    });
    set((s) => ({
      sandboxes: s.sandboxes.map((x) => (x.id === id ? item : x)),
    }));
  },
  deleteSandbox: async (id) => {
    await request(`/api/sandboxes/${id}`, { method: "DELETE" });
    set((s) => ({
      sandboxes: s.sandboxes.filter((x) => x.id !== id),
      sandboxId: s.sandboxId === id ? null : s.sandboxId,
    }));
  },
  fetchFileTree: async () => {
    const id = get().sandboxId;
    if (!id) {
      set({ fileTree: [] });
      return;
    }
    set({ isLoadingTree: true });
    try {
      const d = await request<{ tree: FileNode[] }>(
        `/api/sandboxes/${id}/files`,
      );
      set({ fileTree: visibleWorkspaceTree(d.tree) });
    } catch (e) {
      set({ error: String(e) });
    } finally {
      set({ isLoadingTree: false });
    }
  },
  fetchFileContent: async (path) => {
    const id = get().sandboxId;
    if (!id) return;
    set({ activeFile: path, activeFileContent: null });
    try {
      const d = await request<{ content: string }>(
        `/api/sandboxes/${id}/file?path=${encodeURIComponent(path)}`,
      );
      set({ activeFileContent: d.content });
    } catch (e) {
      set({ error: String(e) });
    }
  },
  saveFileContent: async (path, content) => {
    const id = get().sandboxId;
    if (!id) return;
    await request(`/api/sandboxes/${id}/file`, {
      method: "PUT",
      body: JSON.stringify({ path, content }),
    });
    set({ activeFileContent: content });
    await get().fetchFileTree();
  },
}));
