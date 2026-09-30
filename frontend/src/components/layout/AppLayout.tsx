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
import { MobileTabBar } from "@/components/layout/MobileTabBar";
import { FileExplorer } from "@/components/file-explorer/FileExplorer";
import { useUIStore } from "@/store/uiStore";
import { useSandboxStore } from "@/store/sandboxStore";
import { useChatStore } from "@/store/chatStore";
import { useT } from "@/store/settingsStore";
import { useLocation } from "react-router-dom";
import { isBrainRoute, isWorkspaceRoute } from "@/lib/appRoutes";

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
              {showWorkspacePage ? (
                <WorkspacePage />
              ) : showBrainPage ? (
                <BrainPage />
              ) : (
                <ChatArea />
              )}
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
        <div className="flex md:hidden flex-1 flex-col min-h-0">
          {showWorkspacePage ? (
            <WorkspacePage />
          ) : showBrainPage ? (
            <BrainPage />
          ) : (
            <>
              {mobileTab === "chat" && <ChatArea />}
              {mobileTab === "files" && (
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
              {mobileTab === "terminal" && (
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
              <MobileTabBar />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
