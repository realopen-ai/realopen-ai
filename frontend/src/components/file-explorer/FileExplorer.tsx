import { useState, useCallback, useEffect, useRef } from "react";
import {
  ChevronRight,
  ChevronDown,
  Folder,
  FolderOpen,
  FileCode2,
  FileJson,
  FileType,
  Image,
  Settings,
  FileText,
  Download,
  Upload,
  X,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useSandboxStore, type FileNode } from "@/store/sandboxStore";
import { cn } from "@/lib/utils";

function getFileIcon(name: string) {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, typeof FileCode2> = {
    py: FileCode2,
    js: FileCode2,
    ts: FileCode2,
    tsx: FileCode2,
    jsx: FileCode2,
    json: FileJson,
    csv: FileText,
    md: FileText,
    txt: FileText,
    yml: Settings,
    yaml: Settings,
    toml: Settings,
    png: Image,
    jpg: Image,
    svg: Image,
    gif: Image,
  };
  return map[ext] ?? FileType;
}

function getFileColor(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = {
    py: "text-emerald-400",
    js: "text-amber-400",
    ts: "text-blue-400",
    tsx: "text-blue-400",
    json: "text-amber-400",
    csv: "text-green-400",
    md: "text-slate-400",
    yml: "text-purple-400",
    yaml: "text-purple-400",
  };
  return map[ext] ?? "text-muted-foreground/60";
}

function formatSize(size?: number): string {
  if (!size) return "";
  if (size < 1024) return `${size}B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)}KB`;
  return `${(size / (1024 * 1024)).toFixed(1)}MB`;
}

function FileTreeItem({
  node,
  depth = 0,
  activePath,
  onFileClick,
}: {
  node: FileNode;
  depth?: number;
  activePath: string | null;
  onFileClick: (node: FileNode) => void;
}) {
  const [expanded, setExpanded] = useState(depth < 2);
  const isDir = node.type === "directory";
  const isActive = node.path === activePath;

  const handleClick = () => {
    if (isDir) setExpanded(!expanded);
    else onFileClick(node);
  };

  const Icon = isDir
    ? expanded
      ? FolderOpen
      : Folder
    : getFileIcon(node.name);
  const iconColor = isDir ? "text-amber-400/70" : getFileColor(node.name);

  return (
    <div>
      <div
        onClick={handleClick}
        className={cn(
          "flex items-center gap-1.5 py-1.25 px-2 rounded-lg cursor-pointer select-none transition-colors",
          isActive
            ? "bg-primary/10 text-primary"
            : "text-muted-foreground hover:bg-accent hover:text-foreground",
        )}
        style={{ paddingLeft: `${depth * 12 + 8}px` }}
      >
        {isDir ? (
          expanded ? (
            <ChevronDown className="w-3 h-3 text-muted-foreground/40 shrink-0" />
          ) : (
            <ChevronRight className="w-3 h-3 text-muted-foreground/40 shrink-0" />
          )
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <Icon className={cn("w-3.5 h-3.5 shrink-0", iconColor)} />
        <span className="text-[12px] truncate flex-1">{node.name}</span>
        {!isDir && node.size && (
          <span className="text-[10px] text-muted-foreground/40 shrink-0">
            {formatSize(node.size)}
          </span>
        )}
      </div>
      {isDir && expanded && node.children && (
        <div>
          {node.children
            .sort((a, b) => {
              if (a.type !== b.type) return a.type === "directory" ? -1 : 1;
              return a.name.localeCompare(b.name);
            })
            .map((child) => (
              <FileTreeItem
                key={child.path}
                node={child}
                depth={depth + 1}
                activePath={activePath}
                onFileClick={onFileClick}
              />
            ))}
        </div>
      )}
    </div>
  );
}

export function FileExplorer() {
  const fileTree = useSandboxStore((s) => s.fileTree);
  const activeFile = useSandboxStore((s) => s.activeFile);
  const fetchFileContent = useSandboxStore((s) => s.fetchFileContent);
  const isLoadingTree = useSandboxStore((s) => s.isLoadingTree);
  const sandboxId = useSandboxStore((s) => s.sandboxId);
  const fetchFileTree = useSandboxStore((s) => s.fetchFileTree);
  const uploadRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (sandboxId) void fetchFileTree();
  }, [sandboxId, fetchFileTree]);

  const handleFileClick = useCallback(
    (node: FileNode) => {
      if (node.type === "file") fetchFileContent(node.path);
    },
    [fetchFileContent],
  );

  return (
    <div className="flex flex-col h-full">
      {sandboxId && (
        <div className="flex justify-end gap-1 border-b border-border/50 p-1.5">
          <input
            ref={uploadRef}
            type="file"
            className="hidden"
            onChange={async (e) => {
              const file = e.target.files?.[0];
              if (!file) return;
              const form = new FormData();
              form.append("upload", file);
              await fetch(`/api/sandboxes/${sandboxId}/upload`, {
                method: "POST",
                body: form,
              });
              await fetchFileTree();
              e.target.value = "";
            }}
          />
          <button
            onClick={() => uploadRef.current?.click()}
            className="rounded p-1.5 text-muted-foreground hover:bg-accent"
            title="Upload file"
          >
            <Upload className="h-3.5 w-3.5" />
          </button>
          {activeFile && (
            <a
              href={`/api/sandboxes/${sandboxId}/download?path=${encodeURIComponent(activeFile)}`}
              className="rounded p-1.5 text-muted-foreground hover:bg-accent"
              title="Download file"
            >
              <Download className="h-3.5 w-3.5" />
            </a>
          )}
        </div>
      )}
      <div className="flex-1 min-h-0">
        <ScrollArea className="h-full">
          <div className="p-1.5">
            {isLoadingTree ? (
              <div className="flex items-center justify-center py-8">
                <div className="w-4 h-4 border-2 border-primary border-t-transparent rounded-full animate-spin" />
              </div>
            ) : fileTree.length === 0 ? (
              <div className="text-center py-8">
                <p className="text-[12px] text-muted-foreground/40">
                  {sandboxId
                    ? "No files in workspace"
                    : "Link a workspace to browse files"}
                </p>
              </div>
            ) : (
              fileTree.map((node) => (
                <FileTreeItem
                  key={node.path}
                  node={node}
                  activePath={activeFile}
                  onFileClick={handleFileClick}
                />
              ))
            )}
          </div>
        </ScrollArea>
      </div>
    </div>
  );
}

export function FileEditorPane() {
  const activeFile = useSandboxStore((s) => s.activeFile);
  const activeFileContent = useSandboxStore((s) => s.activeFileContent);
  const setActiveFile = useSandboxStore((s) => s.setActiveFile);
  const saveFileContent = useSandboxStore((s) => s.saveFileContent);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    setDraft(activeFileContent ?? "");
  }, [activeFileContent, activeFile]);

  if (!activeFile) {
    return (
      <div className="flex h-full items-center justify-center bg-background text-xs text-muted-foreground/60">
        Select a file to preview
      </div>
    );
  }

  const filename = activeFile.split("/").pop() ?? activeFile;
  const Icon = getFileIcon(filename);
  const changed = activeFileContent !== null && draft !== activeFileContent;

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <div className="flex h-9 shrink-0 items-center justify-between border-b border-border/50 bg-card">
        <div className="flex h-full min-w-0 items-center gap-2 border-r border-border/50 bg-background px-3">
          <Icon
            className={cn("h-3.5 w-3.5 shrink-0", getFileColor(filename))}
          />
          <span className="truncate text-[11px] font-medium">{activeFile}</span>
          {changed && (
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
          )}
          <button
            type="button"
            onClick={() => setActiveFile(null)}
            className="ml-1 text-muted-foreground/50 hover:text-foreground"
            aria-label="Close file"
          >
            <X className="h-3 w-3 cursor-pointer" />
          </button>
        </div>
        <button
          type="button"
          disabled={!changed}
          onClick={() => void saveFileContent(activeFile, draft)}
          className="mr-2 rounded-md bg-primary px-2.5 py-1 text-[10px] text-primary-foreground disabled:opacity-35"
        >
          Save
        </button>
      </div>
      <textarea
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        className="h-full min-h-0 w-full flex-1 resize-none bg-background p-4 font-mono text-[12px] leading-relaxed text-foreground/85 outline-none"
        spellCheck={false}
        aria-label={`Edit ${activeFile}`}
      />
    </div>
  );
}
