import { useState, useCallback } from "react";
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
  const activeFileContent = useSandboxStore((s) => s.activeFileContent);
  const setActiveFile = useSandboxStore((s) => s.setActiveFile);
  const fetchFileContent = useSandboxStore((s) => s.fetchFileContent);
  const isLoadingTree = useSandboxStore((s) => s.isLoadingTree);

  const handleFileClick = useCallback(
    (node: FileNode) => {
      if (node.type === "file") fetchFileContent(node.path);
    },
    [fetchFileContent],
  );

  return (
    <div className="flex flex-col h-full">
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
                  No files in sandbox
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

      {activeFile && (
        <div className="border-t border-border/50 flex flex-col max-h-[45%]">
          <div className="flex items-center justify-between px-3 py-1.5 border-b border-border/50 bg-secondary/30">
            <div className="flex items-center gap-1.5 min-w-0">
              {(() => {
                const I = getFileIcon(activeFile.split("/").pop() ?? "");
                return (
                  <I className="w-3 h-3 shrink-0 text-muted-foreground/60" />
                );
              })()}
              <span className="text-[10px] text-muted-foreground truncate">
                {activeFile.split("/").pop()}
              </span>
            </div>
            <button
              onClick={() => setActiveFile(null)}
              className="text-muted-foreground/50 hover:text-foreground text-[11px]"
            >
              ✕
            </button>
          </div>
          <ScrollArea className="flex-1">
            <pre className="p-3 text-[11px] font-mono text-foreground/70 leading-relaxed overflow-x-auto">
              {activeFileContent ?? "Loading..."}
            </pre>
          </ScrollArea>
        </div>
      )}
    </div>
  );
}
