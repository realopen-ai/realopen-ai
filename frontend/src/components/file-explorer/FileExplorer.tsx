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
  Pencil,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/primitives";
import { useSandboxStore, type FileNode } from "@/store/sandboxStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import { HighlightedCode } from "@/components/ui/HighlightedCode";

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
  return map[ext] ?? "text-muted-foreground/80";
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
      <button
        type="button"
        onClick={handleClick}
        title={node.name}
        aria-expanded={isDir ? expanded : undefined}
        className={cn(
          "group relative flex h-7.5 w-full cursor-pointer select-none items-center gap-1.5 rounded-md pl-1.5 pr-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
          isActive
            ? "bg-primary/10 text-primary"
            : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
        )}
      >
        {isDir ? (
          expanded ? (
            <ChevronDown className="size-3 shrink-0 text-muted-foreground/70" />
          ) : (
            <ChevronRight className="size-3 shrink-0 text-muted-foreground/70" />
          )
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <Icon className={cn("size-3.75 shrink-0", iconColor)} />
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-[13px]",
            isDir && !isActive && "font-medium text-foreground/85",
          )}
        >
          {node.name}
        </span>
        {!isDir && node.size && (
          <span
            className={cn(
              "pointer-events-none hidden shrink-0 whitespace-nowrap text-[10.5px] group-hover:inline-block group-focus-visible:inline-block",
              isActive ? "text-primary/70" : "text-muted-foreground/80",
            )}
          >
            {formatSize(node.size)}
          </span>
        )}
      </button>
      {isDir && expanded && node.children && (
        <div className="ml-4.5 border-l border-border/40 pl-1">
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
  const t = useT();

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
        <div className="flex h-8 shrink-0 items-center justify-between gap-1 px-2">
          <span className="truncate text-[12px] font-medium text-muted-foreground/80">
            {t("panel.files")}
          </span>
          <div className="flex items-center gap-0.5">
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
              className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              aria-label={t("panel.uploadFile")}
              title={t("panel.uploadFile")}
            >
              <Upload className="size-3.5" />
            </button>
            {activeFile && (
              <a
                href={`/api/sandboxes/${sandboxId}/download?path=${encodeURIComponent(activeFile)}`}
                className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground"
                aria-label={t("panel.downloadFile")}
                title={t("panel.downloadFile")}
              >
                <Download className="size-3.5" />
              </a>
            )}
          </div>
        </div>
      )}
      <div className="flex-1 min-h-0">
        <ScrollArea className="h-full">
          <div className="px-1 py-1.5">
            {isLoadingTree ? (
              <div className="flex items-center justify-center py-10">
                <div className="size-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
              </div>
            ) : fileTree.length === 0 ? (
              <div className="flex flex-col items-center gap-2.5 px-4 py-10 text-center">
                <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-secondary text-muted-foreground [&_svg]:size-4">
                  {sandboxId ? <FolderOpen /> : <Folder />}
                </div>
                <div>
                  <p className="text-[12.5px] font-medium text-foreground">
                    {sandboxId ? t("panel.noFiles") : t("panel.noWorkspace")}
                  </p>
                  <p className="mt-0.5 text-[11.5px] leading-relaxed text-muted-foreground">
                    {sandboxId
                      ? t("panel.noFilesDescription")
                      : t("panel.linkWorkspaceDescription")}
                  </p>
                </div>
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
  const t = useT();
  const activeFile = useSandboxStore((s) => s.activeFile);
  const activeFileContent = useSandboxStore((s) => s.activeFileContent);
  const setActiveFile = useSandboxStore((s) => s.setActiveFile);
  const saveFileContent = useSandboxStore((s) => s.saveFileContent);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    setDraft(activeFileContent ?? "");
    setEditing(false);
  }, [activeFileContent, activeFile]);

  if (!activeFile) {
    return (
      <div className="flex h-full items-center justify-center bg-sandbox-bg">
        <EmptyState
          icon={<FileCode2 />}
          title={t("panel.selectFile")}
          description={t("panel.selectFileDescription")}
          className="py-0"
        />
      </div>
    );
  }

  const filename = activeFile.split("/").pop() ?? activeFile;
  const dirPath = activeFile.slice(0, activeFile.length - filename.length);
  const Icon = getFileIcon(filename);
  const changed = activeFileContent !== null && draft !== activeFileContent;

  return (
    <div className="flex h-full min-h-0 flex-col bg-sandbox-bg">
      <div className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-border/60 px-3">
        <div className="flex min-w-0 items-center gap-1.5">
          <Icon className={cn("size-3.5 shrink-0", getFileColor(filename))} />
          {dirPath && (
            <span className="truncate text-[12px] text-muted-foreground/70">
              {dirPath}
            </span>
          )}
          <span className="truncate text-[12.5px] font-medium text-foreground">
            {filename}
          </span>
          {changed && (
            <span
              className="size-1.5 shrink-0 rounded-full bg-primary"
              title={t("panel.unsavedChanges")}
            />
          )}
          <button
            type="button"
            onClick={() => setActiveFile(null)}
            className="ml-0.5 -mr-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground/70 transition-colors hover:bg-surface-hover hover:text-foreground"
            aria-label={t("panel.closeFile")}
            title={t("panel.closeFile")}
          >
            <X className="size-3.5" />
          </button>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            variant="ghost"
            size="xs"
            className="h-6 px-2"
            onClick={() => setEditing((value) => !value)}
          >
            <Pencil className="size-3" />
            {editing ? t("panel.preview") : t("panel.edit")}
          </Button>
          <Button
            variant="default"
            size="xs"
            className="h-6 px-2.5"
            onClick={() => void saveFileContent(activeFile, draft)}
            disabled={!changed}
          >
            {t("panel.save")}
          </Button>
        </div>
      </div>
      {editing ? (
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="h-full min-h-0 w-full flex-1 resize-none bg-transparent p-4 font-mono text-[12.5px] leading-[1.7] text-foreground/90 outline-none"
          spellCheck={false}
          aria-label={`${t("panel.edit")} ${activeFile}`}
        />
      ) : (
        <pre className="m-0 min-h-0 flex-1 overflow-auto p-4 font-mono text-[12.5px] leading-[1.7] text-foreground/90">
          <HighlightedCode code={draft} filePath={activeFile} />
        </pre>
      )}
    </div>
  );
}
