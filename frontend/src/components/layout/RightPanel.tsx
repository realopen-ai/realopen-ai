import { lazy, Suspense, useEffect, useState } from "react";
import {
  PanelRightClose,
  PanelRightOpen,
  Code2,
  RefreshCw,
  Plus,
  Play,
  Square,
  MonitorPlay,
  ExternalLink,
  GripVertical,
  GripHorizontal,
  Terminal as TerminalIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useUIStore } from "@/store/uiStore";
import { useSandboxStore } from "@/store/sandboxStore";
import {
  FileEditorPane,
  FileExplorer,
} from "@/components/file-explorer/FileExplorer";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import { CreateSandboxDialog } from "@/components/workspace/SandboxDialogs";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";

const TerminalPane = lazy(() =>
  import("@/components/terminal/TerminalPane").then((m) => ({
    default: m.TerminalPane,
  })),
);

function TerminalLoader() {
  return (
    <div className="flex items-center justify-center h-full bg-terminal-bg">
      <div className="flex items-center gap-2 text-muted-foreground/50">
        <div className="w-4 h-4 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span className="text-[12px]">Loading terminal...</span>
      </div>
    </div>
  );
}

export function RightPanel() {
  const [previewKey, setPreviewKey] = useState(0);
  const [createSandboxOpen, setCreateSandboxOpen] = useState(false);
  const [terminalTab, setTerminalTab] = useState<"coder" | "shell">("coder");
  const toggleRightPanel = useUIStore((s) => s.toggleRightPanel);
  const rightPanelTab = useUIStore((s) => s.rightPanelTab);
  const setRightPanelTab = useUIStore((s) => s.setRightPanelTab);
  const fetchFileTree = useSandboxStore((s) => s.fetchFileTree);
  const isLoadingTree = useSandboxStore((s) => s.isLoadingTree);
  const sandboxId = useSandboxStore((s) => s.sandboxId);
  const sandboxes = useSandboxStore((s) => s.sandboxes);
  const loadSandboxes = useSandboxStore((s) => s.loadSandboxes);
  const loadForConversation = useSandboxStore((s) => s.loadForConversation);
  const linkSandbox = useSandboxStore((s) => s.linkSandbox);
  const lifecycle = useSandboxStore((s) => s.lifecycle);
  const conversationId = useChatStore((s) => s.activeConversationId);
  const previewUrl = useSandboxStore((s) => s.previewUrl);
  useEffect(() => {
    void loadSandboxes();
  }, [loadSandboxes]);
  useEffect(() => {
    void loadForConversation(conversationId);
  }, [conversationId, loadForConversation]);
  const active = sandboxes.find((item) => item.id === sandboxId);

  return (
    <div className="flex flex-col h-full bg-card">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border/50">
        <Tabs
          value={rightPanelTab}
          onValueChange={(v) => setRightPanelTab(v as "code" | "preview")}
        >
          <TabsList className="h-7 bg-secondary/50 p-0.5">
            <TabsTrigger
              value="code"
              className="h-6 text-[11px] gap-1 px-2.5 rounded-md data-[state=active]:bg-accent data-[state=active]:shadow-none"
            >
              <Code2 className="w-3 h-3" />
              Code
            </TabsTrigger>
            <TabsTrigger
              value="preview"
              className="h-6 text-[11px] gap-1 px-2.5 rounded-md data-[state=active]:bg-accent data-[state=active]:shadow-none"
            >
              <MonitorPlay className="w-3 h-3" />
              Preview
            </TabsTrigger>
          </TabsList>
        </Tabs>
        <div className="flex items-center gap-0.5">
          {rightPanelTab === "code" && (
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6"
              onClick={fetchFileTree}
              disabled={isLoadingTree}
            >
              <RefreshCw
                className={cn(
                  "w-3 h-3 text-muted-foreground",
                  isLoadingTree && "animate-spin",
                )}
              />
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6"
            onClick={toggleRightPanel}
          >
            <PanelRightClose className="w-3.5 h-3.5 text-muted-foreground" />
          </Button>
        </div>
      </div>
      <div className="flex items-center gap-1.5 px-2 py-1.5 border-b border-border/50 bg-secondary/20">
        <select
          className="h-7 min-w-0 flex-1 rounded-md border border-border bg-background px-2 text-[11px]"
          value={sandboxId ?? ""}
          onChange={(e) =>
            conversationId &&
            e.target.value &&
            void linkSandbox(e.target.value, conversationId)
          }
          disabled={!conversationId}
        >
          <option value="">No workspace linked</option>
          {sandboxes.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name} · {item.status}
            </option>
          ))}
        </select>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          title="Create workspace"
          onClick={() => setCreateSandboxOpen(true)}
        >
          <Plus className="w-3.5 h-3.5" />
        </Button>
        {active && (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            title={
              active.status === "running" ? "Stop workspace" : "Start workspace"
            }
            onClick={() =>
              void lifecycle(active.status === "running" ? "stop" : "start")
            }
          >
            {active.status === "running" ? (
              <Square className="w-3 h-3" />
            ) : (
              <Play className="w-3 h-3" />
            )}
          </Button>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden">
        {rightPanelTab === "code" ? (
          <PanelGroup
            direction="horizontal"
            autoSaveId="sandbox-code-columns"
            className="h-full"
          >
            <Panel
              defaultSize={27}
              minSize={16}
              maxSize={48}
              className="min-w-0 bg-card"
            >
              <FileExplorer />
            </Panel>
            <PanelResizeHandle className="group relative w-1 shrink-0 border-x border-border/40 bg-border/20 transition-colors hover:bg-primary/20 data-resize-handle-active:bg-primary/30">
              <div className="absolute left-1/2 top-1/2 flex h-8 w-3 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-border bg-card shadow-sm group-hover:border-primary/50">
                <GripVertical className="h-3 w-3 text-muted-foreground" />
              </div>
            </PanelResizeHandle>
            <Panel minSize={40} className="min-w-0">
              <PanelGroup
                direction="vertical"
                autoSaveId="sandbox-code-rows"
                className="h-full"
              >
                <Panel defaultSize={68} minSize={25} className="min-h-0">
                  <FileEditorPane />
                </Panel>
                <PanelResizeHandle className="group relative h-1 shrink-0 border-y border-border/40 bg-border/20 transition-colors hover:bg-primary/20 data-resize-handle-active:bg-primary/30">
                  <div className="absolute left-1/2 top-1/2 flex h-3 w-8 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-border bg-card shadow-sm group-hover:border-primary/50">
                    <GripHorizontal className="h-3 w-3 text-muted-foreground" />
                  </div>
                </PanelResizeHandle>
                <Panel
                  defaultSize={32}
                  minSize={16}
                  maxSize={70}
                  className="min-h-0 border-t border-border/40"
                >
                  <div className="flex h-full min-h-0 flex-col bg-terminal-bg">
                    <div className="flex h-8 shrink-0 items-center border-b border-white/10 bg-card px-1">
                      <button
                        type="button"
                        onClick={() => setTerminalTab("coder")}
                        className={cn(
                          "flex h-7 items-center gap-1.5 rounded px-2.5 text-[11px] transition-colors",
                          terminalTab === "coder"
                            ? "bg-accent text-foreground"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        <TerminalIcon className="h-3.5 w-3.5" />
                        Terminal
                      </button>
                      <button
                        type="button"
                        onClick={() => setTerminalTab("shell")}
                        className={cn(
                          "flex h-7 items-center gap-1.5 rounded px-2.5 text-[11px] transition-colors",
                          terminalTab === "shell"
                            ? "bg-accent text-foreground"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        <TerminalIcon className="h-3.5 w-3.5" />
                        User shell
                      </button>
                    </div>
                    <div
                      className={cn(
                        "min-h-0 flex-1",
                        terminalTab !== "coder" && "hidden",
                      )}
                    >
                      <Suspense fallback={<TerminalLoader />}>
                        <TerminalPane
                          mode="coder"
                          active={terminalTab === "coder"}
                        />
                      </Suspense>
                    </div>
                    <div
                      className={cn(
                        "min-h-0 flex-1",
                        terminalTab !== "shell" && "hidden",
                      )}
                    >
                      <Suspense fallback={<TerminalLoader />}>
                        <TerminalPane
                          mode="shell"
                          active={terminalTab === "shell"}
                        />
                      </Suspense>
                    </div>
                  </div>
                </Panel>
              </PanelGroup>
            </Panel>
          </PanelGroup>
        ) : previewUrl ? (
          <div className="flex h-full flex-col bg-background">
            <div className="flex items-center justify-between border-b border-border/50 px-2 py-1.5">
              <span className="truncate font-mono text-[10px] text-muted-foreground">
                {previewUrl}
              </span>
              <div className="flex items-center">
                <button
                  type="button"
                  onClick={() => setPreviewKey((value) => value + 1)}
                  className="rounded p-1 hover:bg-accent"
                  title="Reload preview"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                </button>
                <a
                  href={previewUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="rounded p-1 hover:bg-accent"
                  title="Open preview in new tab"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>
              </div>
            </div>
            <iframe
              key={previewKey}
              title="Sandbox app preview"
              src={previewUrl}
              className="h-full w-full flex-1 border-0 bg-white"
              sandbox="allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
            />
          </div>
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-muted-foreground">
            <MonitorPlay className="h-8 w-8 opacity-40" />
            <p className="text-xs">
              The app preview will appear here after the coder starts it.
            </p>
          </div>
        )}
      </div>
      <CreateSandboxDialog
        open={createSandboxOpen}
        onOpenChange={setCreateSandboxOpen}
        conversationId={conversationId}
      />
    </div>
  );
}

export function RightPanelToggle() {
  const toggleRightPanel = useUIStore((s) => s.toggleRightPanel);
  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-7 w-7"
      onClick={toggleRightPanel}
      title="Open sandbox panel"
    >
      <PanelRightOpen className="w-4 h-4 text-muted-foreground hover:text-foreground" />
    </Button>
  );
}
