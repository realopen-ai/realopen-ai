import { useState, useEffect, useRef, lazy, Suspense } from "react";
import {
  Panel,
  PanelGroup,
  PanelResizeHandle,
  type ImperativePanelHandle,
} from "react-resizable-panels";
import { Sidebar, MobileMenuButton } from "@/components/layout/Sidebar";
import { RightPanel, RightPanelToggle } from "@/components/layout/RightPanel";
import { ChatArea } from "@/components/chat/ChatArea";
import { BrainPage } from "@/components/brain/BrainPage";
import { WorkspacePage } from "@/components/workspace/WorkspacePage";
import { LearnPage } from "@/components/learn/LearnPage";
import { MobileTabBar } from "@/components/layout/MobileTabBar";
import { FileExplorer } from "@/components/file-explorer/FileExplorer";
import { useUIStore } from "@/store/uiStore";
import { useSandboxStore } from "@/store/sandboxStore";
import { useChatStore } from "@/store/chatStore";
import { useT } from "@/store/settingsStore";
import { useLearnStore } from "@/store/learnStore";
import { useLocation } from "react-router-dom";
import { isBrainRoute, isWorkspaceRoute, isLearnRoute } from "@/lib/appRoutes";

const TerminalPane = lazy(() =>
  import("@/components/terminal/TerminalPane").then((m) => ({
    default: m.TerminalPane,
  })),
);

function TerminalLoader() {
  return (
    <div className="flex items-center justify-center h-full bg-terminal-bg">
      <div className="flex items-center gap-2 text-muted-foreground/70">
        <div className="w-4 h-4 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span className="text-[12px]">Loading terminal...</span>
      </div>
    </div>
  );
}

export function AppLayout() {
  const rightPanelOpen = useUIStore((s) => s.rightPanelOpen);
  const setRightPanelOpen = useUIStore((s) => s.setRightPanelOpen);
  const mobileTab = useUIStore((s) => s.mobileTab);
  const { pathname } = useLocation();
  const showBrainPage = isBrainRoute(pathname);
  const showWorkspacePage = isWorkspaceRoute(pathname);
  const showLearnPage = isLearnRoute(pathname);
  const [desktop, setDesktop] = useState(
    () => window.matchMedia("(min-width: 768px)").matches,
  );
  useEffect(() => {
    const query = window.matchMedia("(min-width: 768px)");
    const listener = () => setDesktop(query.matches);
    query.addEventListener("change", listener);
    return () => query.removeEventListener("change", listener);
  }, []);
  useEffect(() => {
    if (showLearnPage || !rightPanelOpen || !desktop)
      useLearnStore.getState().setStudyDeck(null);
  }, [showLearnPage, rightPanelOpen, desktop]);
  const fetchFileTree = useSandboxStore((s) => s.fetchFileTree);
  const t = useT();

  const rightPanelRef = useRef<ImperativePanelHandle>(null);
  const initialSyncDone = useRef(false);

  // Track whether the user is actively dragging the resize handle.
  // When NOT dragging, apply CSS transition for smooth expand/collapse.
  // When dragging, no transition so resize feels instant.
  const [isResizing, setIsResizing] = useState(true);

  useEffect(() => {
    setIsResizing(true);
    const timeout = setTimeout(() => setIsResizing(false), 100);
    return () => clearTimeout(timeout);
  }, []);

  useEffect(() => {
    fetchFileTree();
    // Load models from backend
    import("@/api/client").then(({ fetchModels: fm }) => {
      fm().then(({ models, profile, label }) => {
        useChatStore.getState().setModels(models);
        useChatStore.getState().setProfileName(profile);
        useChatStore.getState().setProfileLabel(label);
      });
    });
    // Load module info from backend
    useChatStore.getState().loadModules();
    // Load conversations from backend
    useChatStore.getState().loadConversations();
  }, [fetchFileTree]);

  // Sync right panel open/close with imperative handle
  useEffect(() => {
    const panel = rightPanelRef.current;
    if (!panel) return;

    if (!initialSyncDone.current) {
      initialSyncDone.current = true;
      // On initial mount, sync without animation
      if (!rightPanelOpen && !panel.isCollapsed()) {
        panel.collapse();
      } else if (rightPanelOpen && panel.isCollapsed()) {
        panel.expand();
      }
      return;
    }

    if (rightPanelOpen && panel.isCollapsed()) {
      panel.expand();
    } else if (!rightPanelOpen && !panel.isCollapsed()) {
      panel.collapse();
    }
  }, [rightPanelOpen]);

  // Transition class applied to panels only when NOT manually resizing
  const panelTransitionClass = !isResizing
    ? "transition-all duration-100 ease-in-out"
    : "";

  return (
    <div className="flex h-full w-full overflow-hidden bg-background">
      <Sidebar />

      <div className="flex-1 flex flex-col min-w-0">
        {/* Desktop: resizable panes */}
        <div className="hidden md:flex flex-1 min-h-0">
          <PanelGroup direction="horizontal" autoSaveId="main-layout">
            <Panel
              defaultSize={60}
              minSize={35}
              className={panelTransitionClass}
            >
              <div className="relative h-full overflow-hidden">
                {/* Keep ChatArea mounted while browsing application routes.
                    It owns live text streams and the voice WebSocket, so
                    replacing it here would terminate in-flight work. */}
                <div
                  className="h-full"
                  inert={showLearnPage}
                  aria-hidden={showLearnPage || undefined}
                >
                  <ChatArea />
                </div>
                {showLearnPage && desktop && (
                  <div className="absolute inset-0 z-30">
                    <LearnPage />
                  </div>
                )}
                {showWorkspacePage && (
                  <div className="absolute inset-0 z-30">
                    <WorkspacePage />
                  </div>
                )}
                {showBrainPage && (
                  <div className="absolute inset-0 z-30">
                    <BrainPage />
                  </div>
                )}
              </div>
            </Panel>

            <PanelResizeHandle
              className="w-px bg-border/50 transition-colors hover:bg-primary/50 active:bg-primary/70"
              onDragging={(dragging) => setIsResizing(dragging)}
            />

            <Panel
              ref={rightPanelRef}
              defaultSize={40}
              minSize={25}
              maxSize={55}
              collapsible
              collapsedSize={0}
              className={panelTransitionClass}
              onCollapse={() => setRightPanelOpen(false)}
              onExpand={() => setRightPanelOpen(true)}
            >
              <div className="h-full overflow-hidden">
                <RightPanel />
              </div>
            </Panel>
          </PanelGroup>

          {!rightPanelOpen && (
            <div className="flex shrink-0 items-start justify-center pt-2 pr-1 pl-0.5">
              <RightPanelToggle />
            </div>
          )}
        </div>

        {/* Mobile: tabbed views */}
        <div className="relative flex md:hidden flex-1 flex-col min-h-0 overflow-hidden">
          {/* `hidden` preserves the mounted chat runtime when the user opens
              a mobile utility tab. Routed pages are layered above it so an
              active voice call remains controllable. */}
          <div
            className={mobileTab === "chat" ? "h-full" : "hidden"}
            inert={showLearnPage}
            aria-hidden={showLearnPage || undefined}
          >
            <ChatArea />
          </div>

          {!showLearnPage &&
            !showWorkspacePage &&
            !showBrainPage &&
            mobileTab === "files" && (
              <div className="flex-1 flex flex-col bg-card">
                <div className="flex h-11 items-center border-b border-border/60 px-2">
                  <MobileMenuButton />
                  <h2 className="ml-1 text-[13.5px] font-medium text-foreground">
                    {t("panel.fileExplorer")}
                  </h2>
                </div>
                <div className="flex-1 overflow-hidden">
                  <FileExplorer />
                </div>
              </div>
            )}
          {!showLearnPage &&
            !showWorkspacePage &&
            !showBrainPage &&
            mobileTab === "terminal" && (
              <div className="flex-1 flex flex-col bg-card">
                <div className="flex h-11 items-center border-b border-border/60 px-2">
                  <MobileMenuButton />
                  <h2 className="ml-1 text-[13.5px] font-medium text-foreground">
                    {t("panel.terminal")}
                  </h2>
                </div>
                <div className="flex-1 overflow-hidden">
                  <Suspense fallback={<TerminalLoader />}>
                    <TerminalPane />
                  </Suspense>
                </div>
              </div>
            )}

          {showWorkspacePage && (
            <div className="absolute inset-0 z-30">
              <WorkspacePage />
            </div>
          )}
          {showBrainPage && (
            <div className="absolute inset-0 z-30">
              <BrainPage />
            </div>
          )}
          {showLearnPage && !desktop && (
            <div className="absolute inset-0 z-30">
              <LearnPage />
            </div>
          )}
          {!showLearnPage && !showWorkspacePage && !showBrainPage && (
            <MobileTabBar />
          )}
        </div>
      </div>
    </div>
  );
}
