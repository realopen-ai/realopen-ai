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

export function mergeSandboxList(
  remote: Sandbox[],
  activeId: string | null,
  current: Sandbox[],
): Sandbox[] {
  if (!activeId || remote.some((item) => item.id === activeId)) return remote;
  const active = current.find((item) => item.id === activeId);
  return active ? [active, ...remote] : remote;
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
  previewPort: number | null;
  terminalHistory: string[];
  loading: boolean;
  lifecyclePending: Record<string, "start" | "stop" | "restart">;
  error: string | null;
  setFileTree: (tree: FileNode[]) => void;
  setLoadingTree: (v: boolean) => void;
  setActiveFile: (path: string | null, content?: string | null) => void;
  addTerminalLine: (line: string) => void;
  clearTerminal: () => void;
  setSandboxId: (id: string | null) => void;
  setPreviewUrl: (url: string | null) => void;
  selectPreview: (url: string, port?: number) => void;
  loadCommandHistory: () => Promise<void>;
  activateSandbox: (sandboxId: string, sandbox?: Sandbox) => Promise<void>;
  loadSandboxes: () => Promise<void>;
  loadForConversation: (conversationId: string | null) => Promise<void>;
  discoverPreview: (sandboxId: string) => Promise<void>;
  createSandbox: (options: SandboxCreateOptions) => Promise<Sandbox>;
  linkSandbox: (sandboxId: string, conversationId: string) => Promise<void>;
  lifecycle: (
    action: "start" | "stop" | "restart",
    sandboxId?: string,
  ) => Promise<void>;
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
  previewPort: null,
  terminalHistory: [],
  loading: false,
  lifecyclePending: {},
  error: null,
  setFileTree: (fileTree) => set({ fileTree }),
  setLoadingTree: (isLoadingTree) => set({ isLoadingTree }),
  setActiveFile: (activeFile, content) =>
    set({ activeFile, activeFileContent: content ?? null }),
  addTerminalLine: (line) =>
    set((s) => ({ terminalHistory: [...s.terminalHistory, line] })),
  clearTerminal: () => set({ terminalHistory: [] }),
  setPreviewUrl: (previewUrl) => set({ previewUrl, previewPort: null }),
  selectPreview: (previewUrl, port) =>
    set((state) => {
      const previewPort = port ?? null;
      if (state.previewPort === 6767 && previewPort !== 6767) return state;
      return { previewUrl, previewPort };
    }),
  setSandboxId: (sandboxId) => {
    conversationLoadRevision += 1;
    set({
      sandboxId,
      fileTree: [],
      activeFile: null,
      terminalHistory: [],
      previewUrl: null,
      previewPort: null,
    });
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
  activateSandbox: async (sandboxId, sandbox) => {
    conversationLoadRevision += 1;
    set((state) => ({
      ...(state.sandboxId !== sandboxId
        ? {
            sandboxId,
            fileTree: [],
            activeFile: null,
            activeFileContent: null,
            terminalHistory: [],
            error: null,
            previewUrl: null,
            previewPort: null,
          }
        : {}),
      ...(sandbox
        ? {
            sandboxes: [
              sandbox,
              ...state.sandboxes.filter((item) => item.id !== sandbox.id),
            ],
          }
        : {}),
    }));
    const loadActiveSandbox = async () => {
      try {
        const active = await request<Sandbox>(`/api/sandboxes/${sandboxId}`);
        set((state) => ({
          sandboxes: [
            active,
            ...state.sandboxes.filter((item) => item.id !== active.id),
          ],
        }));
      } catch (error) {
        set({ error: String(error) });
      }
    };
    await Promise.all([
      loadActiveSandbox(),
      get().loadSandboxes(),
      get().fetchFileTree(),
      get().loadCommandHistory(),
    ]);
  },
  loadSandboxes: async () => {
    set({ loading: true, error: null });
    try {
      const d = await request<{ sandboxes: Sandbox[] }>("/api/sandboxes");
      set((state) => ({
        sandboxes: mergeSandboxList(
          d.sandboxes,
          state.sandboxId,
          state.sandboxes,
        ),
      }));
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
      set((state) => ({
        sandboxId: d.sandbox?.id ?? null,
        terminalHistory: [],
        ...(state.sandboxId !== (d.sandbox?.id ?? null)
          ? { previewUrl: null, previewPort: null }
          : {}),
        sandboxes: d.sandbox
          ? [
              d.sandbox,
              ...state.sandboxes.filter((item) => item.id !== d.sandbox?.id),
            ]
          : state.sandboxes,
      }));
      if (d.sandbox)
        await Promise.all([
          get().fetchFileTree(),
          get().loadCommandHistory(),
          get().discoverPreview(d.sandbox.id),
        ]);
    } catch (e) {
      if (revision === conversationLoadRevision) set({ error: String(e) });
    }
  },
  discoverPreview: async (sandboxId) => {
    try {
      const result = await request<{
        preferred: { port: number; url: string; host_url: string } | null;
      }>(`/api/sandboxes/${sandboxId}/previews`);
      if (result.preferred && get().sandboxId === sandboxId) {
        get().selectPreview(result.preferred.url, result.preferred.port);
      }
    } catch {
      // Preview discovery is best-effort and should not make workspace loading fail.
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
  lifecycle: async (action, sandboxId) => {
    const id = sandboxId ?? get().sandboxId;
    if (!id) return;
    if (get().lifecyclePending[id]) return;
    set((state) => ({
      lifecyclePending: { ...state.lifecyclePending, [id]: action },
      error: null,
    }));
    try {
      const item = await request<Sandbox>(`/api/sandboxes/${id}/${action}`, {
        method: "POST",
      });
      set((state) => ({
        sandboxes: state.sandboxes.map((entry) =>
          entry.id === id ? item : entry,
        ),
      }));
    } catch (error) {
      set({ error: String(error) });
    } finally {
      set((state) => {
        const lifecyclePending = { ...state.lifecyclePending };
        delete lifecyclePending[id];
        return { lifecyclePending };
      });
    }
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
