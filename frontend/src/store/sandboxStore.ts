import { create } from "zustand";

export interface FileNode {
  name: string;
  type: "file" | "directory";
  path: string;
  children?: FileNode[];
  size?: number;
  modifiedAt?: string;
}

interface SandboxState {
  fileTree: FileNode[];
  isLoadingTree: boolean;
  activeFile: string | null;
  activeFileContent: string | null;
  terminalHistory: string[];
  sandboxId: string | null;

  // Actions
  setFileTree: (tree: FileNode[]) => void;
  setLoadingTree: (loading: boolean) => void;
  setActiveFile: (path: string | null, content?: string | null) => void;
  addTerminalLine: (line: string) => void;
  clearTerminal: () => void;
  setSandboxId: (id: string | null) => void;
  fetchFileTree: () => Promise<void>;
  fetchFileContent: (path: string) => Promise<void>;
}

export const useSandboxStore = create<SandboxState>((set, _) => ({
  fileTree: [],
  isLoadingTree: false,
  activeFile: null,
  activeFileContent: null,
  terminalHistory: [],
  sandboxId: null,

  setFileTree: (tree) => set({ fileTree: tree }),
  setLoadingTree: (loading) => set({ isLoadingTree: loading }),
  setActiveFile: (path, content) =>
    set({ activeFile: path, activeFileContent: content ?? null }),
  addTerminalLine: (line) =>
    set((s) => ({ terminalHistory: [...s.terminalHistory, line] })),
  clearTerminal: () => set({ terminalHistory: [] }),
  setSandboxId: (id) => set({ sandboxId: id }),

  fetchFileTree: async () => {
    set({ isLoadingTree: true });
    try {
      const res = await fetch("/api/sandbox/files");
      if (res.ok) {
        const data = await res.json();
        set({ fileTree: data.tree ?? [] });
      } else {
        // Placeholder fallback
        set({
          fileTree: [
            {
              name: "workspace",
              type: "directory",
              path: "/workspace",
              children: [
                {
                  name: "main.py",
                  type: "file",
                  path: "/workspace/main.py",
                  size: 1024,
                  modifiedAt: "2025-04-30T12:00:00Z",
                },
                {
                  name: "utils",
                  type: "directory",
                  path: "/workspace/utils",
                  children: [
                    {
                      name: "helpers.py",
                      type: "file",
                      path: "/workspace/utils/helpers.py",
                      size: 512,
                      modifiedAt: "2025-04-30T11:30:00Z",
                    },
                    {
                      name: "config.json",
                      type: "file",
                      path: "/workspace/utils/config.json",
                      size: 256,
                      modifiedAt: "2025-04-30T11:00:00Z",
                    },
                  ],
                },
                {
                  name: "data",
                  type: "directory",
                  path: "/workspace/data",
                  children: [
                    {
                      name: "input.csv",
                      type: "file",
                      path: "/workspace/data/input.csv",
                      size: 4096,
                      modifiedAt: "2025-04-30T10:00:00Z",
                    },
                    {
                      name: "output.json",
                      type: "file",
                      path: "/workspace/data/output.json",
                      size: 2048,
                      modifiedAt: "2025-04-30T12:05:00Z",
                    },
                  ],
                },
                {
                  name: "requirements.txt",
                  type: "file",
                  path: "/workspace/requirements.txt",
                  size: 128,
                  modifiedAt: "2025-04-30T09:00:00Z",
                },
                {
                  name: "README.md",
                  type: "file",
                  path: "/workspace/README.md",
                  size: 768,
                  modifiedAt: "2025-04-30T09:30:00Z",
                },
              ],
            },
          ],
        });
      }
    } catch {
      set({
        fileTree: [
          {
            name: "workspace",
            type: "directory",
            path: "/workspace",
            children: [
              {
                name: "main.py",
                type: "file",
                path: "/workspace/main.py",
                size: 1024,
              },
              {
                name: "utils",
                type: "directory",
                path: "/workspace/utils",
                children: [
                  {
                    name: "helpers.py",
                    type: "file",
                    path: "/workspace/utils/helpers.py",
                    size: 512,
                  },
                ],
              },
              {
                name: "data",
                type: "directory",
                path: "/workspace/data",
                children: [
                  {
                    name: "input.csv",
                    type: "file",
                    path: "/workspace/data/input.csv",
                    size: 4096,
                  },
                  {
                    name: "output.json",
                    type: "file",
                    path: "/workspace/data/output.json",
                    size: 2048,
                  },
                ],
              },
              {
                name: "requirements.txt",
                type: "file",
                path: "/workspace/requirements.txt",
                size: 128,
              },
              {
                name: "README.md",
                type: "file",
                path: "/workspace/README.md",
                size: 768,
              },
            ],
          },
        ],
      });
    } finally {
      set({ isLoadingTree: false });
    }
  },

  fetchFileContent: async (path) => {
    try {
      const res = await fetch(
        `/api/sandbox/files?path=${encodeURIComponent(path)}`,
      );
      if (res.ok) {
        const data = await res.json();
        set({ activeFile: path, activeFileContent: data.content ?? "" });
      } else {
        // Placeholder
        const placeholders: Record<string, string> = {
          "/workspace/main.py": `import os\nimport json\nfrom utils.helpers import load_data, process_data\n\ndef main():\n    """Main entry point for the analysis pipeline."""\n    print("Loading data...")\n    data = load_data("data/input.csv")\n    print(f"Loaded {len(data)} records")\n    \n    results = process_data(data)\n    \n    with open("data/output.json", "w") as f:\n        json.dump(results, f, indent=2)\n    print("Results saved to data/output.json")\n\nif __name__ == "__main__":\n    main()\n`,
          "/workspace/utils/helpers.py": `import csv\nimport json\n\ndef load_data(path: str) -> list:\n    """Load CSV data from the given path."""\n    with open(path, 'r') as f:\n        reader = csv.DictReader(f)\n        return list(reader)\n\ndef process_data(data: list) -> dict:\n    """Process the loaded data and return results."""\n    total = len(data)\n    return {\n        "total_records": total,\n        "processed": True,\n        "summary": "Data processed successfully"\n    }\n`,
          "/workspace/utils/config.json": `{\n  "model": "qwen3:8b",\n  "temperature": 0.7,\n  "max_tokens": 2048,\n  "sandbox": {\n    "timeout": 30,\n    "memory_limit": "512m"\n  }\n}\n`,
          "/workspace/requirements.txt": `pandas>=2.0.0\nnumpy>=1.24.0\nscikit-learn>=1.3.0\nrequests>=2.31.0\n`,
          "/workspace/README.md": `# Workspace\n\nThis is the sandbox workspace for the AI coding agent.\n\n## Structure\n- \`main.py\` - Main entry point\n- \`utils/\` - Utility functions and config\n- \`data/\` - Input and output data files\n`,
        };
        set({
          activeFile: path,
          activeFileContent:
            placeholders[path] ?? `// File: ${path}\n// Content loading...`,
        });
      }
    } catch {
      set({ activeFile: path, activeFileContent: `// Error loading ${path}` });
    }
  },
}));
