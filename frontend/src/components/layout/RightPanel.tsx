import { lazy, Suspense, useEffect, useState } from "react";
import { StudySession } from "@/components/learn/StudySession";
import { QuizSession } from "@/components/learn/QuizSession";
import { useLearnStore } from "@/store/learnStore";
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
  ChevronDown,
  MoreHorizontal,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { EmptyState, StatusDot } from "@/components/ui/primitives";
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
import { shouldLoadConversationWorkspace } from "@/lib/workspaceActivation";
import { normalizePreviewAddress, previewHostUrl } from "@/lib/previewAddress";
import { useT } from "@/store/settingsStore";

const TerminalPane = lazy(() =>
  import("@/components/terminal/TerminalPane").then((m) => ({
    default: m.TerminalPane,
  })),
);

function TerminalLoader() {
  return (
    <div className="flex items-center justify-center h-full bg-terminal-bg">
      <div className="flex items-center gap-2 text-white/40">
        <div className="w-4 h-4 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span className="text-[12px]">Loading terminal...</span>
      </div>
    </div>
  );
}

/** Map a sandbox status string to StatusDot tone + pulse. */
function sandboxStatusTone(status: string): {
  tone: "success" | "warning" | "danger" | "neutral";
  pulse: boolean;
} {
  const s = status.toLowerCase();
  if (s === "running") return { tone: "success", pulse: true };
  if (
    s === "starting" ||
    s === "stopping" ||
    s === "restarting" ||
    s === "creating"
  )
    return { tone: "warning", pulse: true };
  if (s === "error" || s === "failed") return { tone: "danger", pulse: false };
  return { tone: "neutral", pulse: false };
}

/** Quiet mono tab used for the terminal session switcher. */
function TerminalTab({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "flex h-6 items-center gap-1.5 rounded-md px-2 font-mono text-[12.5px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
        active
          ? "text-white/85"
          : "text-white/50 hover:bg-white/6 hover:text-white/80",
      )}
    >
      <span
        className={cn(
          "h-1.25 w-1.25 shrink-0 rounded-full transition-colors",
          active ? "bg-terminal-green" : "bg-white/20",
        )}
      />
      {label}
    </button>
  );
}

export function RightPanel() {
  const studyDeckId = useLearnStore((s) => s.studyDeckId);
  const quizId = useLearnStore((s) => s.quizId);
  const t = useT();
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
  const lifecyclePending = useSandboxStore((s) => s.lifecyclePending);
  const conversationId = useChatStore((s) => s.activeConversationId);
  const previewUrl = useSandboxStore((s) => s.previewUrl);
  const previewPort = useSandboxStore((s) => s.previewPort);
  const previewBaseUrl = previewHostUrl(previewPort, previewUrl ?? "");
  const [previewAddress, setPreviewAddress] = useState("");
  const [previewDraft, setPreviewDraft] = useState("");
  useEffect(() => {
    void loadSandboxes();
  }, [loadSandboxes]);
  useEffect(() => {
    // A home-page conversation is not active in the URL until its first
    // stream finishes. Its workspace may already have arrived over SSE.
    if (shouldLoadConversationWorkspace(conversationId)) {
      void loadForConversation(conversationId);
    }
  }, [conversationId, loadForConversation]);
  useEffect(() => {
    if (sandboxId) setTerminalTab("coder");
  }, [sandboxId]);
  useEffect(() => {
    setPreviewAddress(previewBaseUrl);
    setPreviewDraft(previewBaseUrl);
  }, [previewBaseUrl]);
  const navigatePreview = () => {
    const next = normalizePreviewAddress(previewDraft, previewBaseUrl);
    if (!next) {
      setPreviewDraft(previewAddress);
      return;
    }
    setPreviewAddress(next);
    setPreviewDraft(next);
    setPreviewKey((value) => value + 1);
  };
  const active = sandboxes.find((item) => item.id === sandboxId);
  const activeLifecycle = active ? lifecyclePending[active.id] : undefined;
  const activeTone = active ? sandboxStatusTone(active.status) : null;

  if (quizId)
    return (
      <QuizSession
        key={quizId}
        quizId={quizId}
        onClose={() => useLearnStore.getState().setQuiz(null)}
      />
    );
  if (studyDeckId)
    return (
      <StudySession
        key={studyDeckId}
        deckId={studyDeckId}
        onClose={() => useLearnStore.getState().setStudyDeck(null)}
      />
    );

  return (
    <div className="flex flex-col h-full bg-card">
      {/* Panel header — quiet view tabs + icon actions, then the sandbox line.
          A single hairline separates the header from the content. */}
      <div className="flex items-center justify-between gap-2 px-2 pb-1 pt-1.5">
        <Tabs
          value={rightPanelTab}
          onValueChange={(v) => setRightPanelTab(v as "code" | "preview")}
        >
          <TabsList className="h-8 gap-0.5 bg-transparent p-0">
            <TabsTrigger
              value="code"
              className="h-7 gap-1.5 rounded-md px-2.5 text-[13.5px] font-medium hover:text-foreground data-[state=active]:bg-surface-selected data-[state=active]:text-foreground data-[state=active]:shadow-none"
            >
              <Code2 className="size-3.5" />
              {t("panel.code")}
            </TabsTrigger>
            <TabsTrigger
              value="preview"
              className="h-7 gap-1.5 rounded-md px-2.5 text-[13.5px] font-medium hover:text-foreground data-[state=active]:bg-surface-selected data-[state=active]:text-foreground data-[state=active]:shadow-none"
            >
              <MonitorPlay className="size-3.5" />
              {t("panel.preview")}
            </TabsTrigger>
          </TabsList>
        </Tabs>
        <div className="flex items-center gap-0.5">
          {rightPanelTab === "code" && (
            <Button
              variant="ghost"
              size="icon-sm"
              className="h-7 w-7"
              onClick={fetchFileTree}
              disabled={isLoadingTree}
              aria-label={t("panel.refreshFiles")}
              title={t("panel.refreshFiles")}
            >
              <RefreshCw
                className={cn("size-3.5", isLoadingTree && "animate-spin")}
              />
            </Button>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon-sm"
                className="h-7 w-7"
                aria-label={t("panel.workspaceActions")}
                title={t("panel.workspaceActions")}
              >
                <MoreHorizontal className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-47.5">
              {/* Defer one tick so the dropdown (a Radix layer that also
                  gates body pointer-events) fully unmounts before the modal
                  dialog mounts — opening both in the same commit leaves
                  body pointer-events:none stuck after the dialog closes. */}
              <DropdownMenuItem
                onSelect={() =>
                  window.setTimeout(() => setCreateSandboxOpen(true), 0)
                }
              >
                <Plus />
                {t("panel.newWorkspace")}
              </DropdownMenuItem>
              {active && (
                <DropdownMenuItem
                  disabled={Boolean(activeLifecycle)}
                  onSelect={() =>
                    void lifecycle(
                      active.status === "running" ? "stop" : "start",
                    )
                  }
                >
                  {active.status === "running" ? <Square /> : <Play />}
                  {active.status === "running"
                    ? t("panel.stopWorkspace")
                    : t("panel.startWorkspace")}
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
          <Button
            variant="ghost"
            size="icon-sm"
            className="h-7 w-7"
            onClick={toggleRightPanel}
            aria-label={t("panel.close")}
            title={t("panel.close")}
          >
            <PanelRightClose className="size-3.5" />
          </Button>
        </div>
      </div>
      <div className="flex items-center gap-1.5 border-b border-border/60 px-2 pb-1.5 pt-0.5">
        <span className="shrink-0 text-[12px] text-muted-foreground/80">
          {t("workspace.title")}
        </span>
        <div className="relative min-w-0 flex-1">
          <select
            aria-label={t("panel.linkedWorkspace")}
            className="h-7 w-full cursor-pointer appearance-none truncate rounded-md bg-transparent pl-1.5 pr-5 text-[12.5px] font-medium text-foreground outline-none transition-colors hover:bg-surface-hover focus-visible:bg-surface-hover disabled:pointer-events-none disabled:opacity-50"
            value={sandboxId ?? ""}
            onChange={(e) =>
              conversationId &&
              e.target.value &&
              void linkSandbox(e.target.value, conversationId)
            }
            disabled={!conversationId || Boolean(activeLifecycle)}
          >
            <option value="">{t("panel.noWorkspace")}</option>
            {sandboxes.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
          <ChevronDown className="pointer-events-none absolute right-1 top-1/2 size-3 -translate-y-1/2 text-muted-foreground/70" />
        </div>
        {active && activeTone && (
          <StatusDot
            tone={activeTone.tone}
            pulse={activeTone.pulse}
            label={
              <span className="capitalize">
                {t(`workspace.status.${active.status}`)}
              </span>
            }
            className="shrink-0"
          />
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden min-h-0">
        {rightPanelTab === "code" ? (
          <PanelGroup
            direction="horizontal"
            autoSaveId="sandbox-code-columns"
            className="h-full"
          >
            <Panel
              defaultSize={36}
              minSize={22}
              maxSize={48}
              className="min-w-0"
            >
              <FileExplorer />
            </Panel>
            {/* Tree | editor resizer: intentionally unstyled — the global
                index.css rules make it a subtle 1px line until hover/drag. */}
            <PanelResizeHandle className="shrink-0" />
            <Panel minSize={40} className="min-w-0">
              <PanelGroup
                direction="vertical"
                autoSaveId="sandbox-code-rows"
                className="h-full"
              >
                <Panel defaultSize={68} minSize={25} className="min-h-0">
                  <FileEditorPane />
                </Panel>
                {/* Code | terminal resizer: quiet hairline that lights up on
                    hover/drag. Doubles as the separator above the terminal.
                    The invisible overlay widens the drag target a little
                    without adding any visible chrome. */}
                <PanelResizeHandle className="group relative h-1.5 shrink-0">
                  <span
                    className="absolute -inset-y-1 inset-x-0"
                    aria-hidden="true"
                  />
                  <span className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border/60 transition-colors group-hover:bg-primary/60 group-data-[resize-handle-state=hover]:bg-primary/60 group-data-[resize-handle-state=drag]:bg-primary/80" />
                </PanelResizeHandle>
                <Panel
                  defaultSize={32}
                  minSize={16}
                  maxSize={70}
                  className="min-h-0"
                >
                  <div className="flex h-full min-h-0 flex-col bg-terminal-bg">
                    <div className="flex h-8 shrink-0 items-center gap-0.5 border-b border-white/8 px-2">
                      <TerminalTab
                        active={terminalTab === "coder"}
                        label="Terminal"
                        onClick={() => setTerminalTab("coder")}
                      />
                      <TerminalTab
                        active={terminalTab === "shell"}
                        label="User shell"
                        onClick={() => setTerminalTab("shell")}
                      />
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
            <form
              className="flex h-8 shrink-0 items-center gap-0.5 border-b border-border/60 px-1.5"
              onSubmit={(event) => {
                event.preventDefault();
                navigatePreview();
              }}
            >
              <input
                aria-label={t("panel.previewAddress")}
                value={previewDraft}
                onChange={(event) => setPreviewDraft(event.target.value)}
                onBlur={navigatePreview}
                className="h-7 min-w-0 flex-1 rounded-md bg-transparent px-2 font-mono text-[12px] text-muted-foreground outline-none transition-colors hover:text-foreground focus:bg-secondary/60 focus:text-foreground"
                spellCheck={false}
              />
              <button
                type="button"
                onClick={navigatePreview}
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground"
                aria-label={t("panel.reloadPreview")}
                title={t("panel.reloadPreview")}
              >
                <RefreshCw className="size-3.5" />
              </button>
              <a
                href={previewAddress}
                target="_blank"
                rel="noreferrer"
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground"
                aria-label={t("panel.openPreview")}
                title={t("panel.openPreview")}
              >
                <ExternalLink className="size-3.5" />
              </a>
            </form>
            <iframe
              key={previewKey}
              title="Sandbox app preview"
              src={previewAddress}
              className="h-full w-full flex-1 border-0 bg-white"
              sandbox="allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
            />
          </div>
        ) : (
          <EmptyState
            icon={<MonitorPlay />}
            title={t("panel.noPreview")}
            description={t("panel.noPreviewDescription")}
            className="h-full"
          />
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
  const t = useT();
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      className="h-7 w-7"
      onClick={toggleRightPanel}
      title={t("panel.open")}
      aria-label={t("panel.open")}
    >
      <PanelRightOpen className="size-4" />
    </Button>
  );
}
