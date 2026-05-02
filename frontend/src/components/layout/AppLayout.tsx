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
import { MobileTabBar } from "@/components/layout/MobileTabBar";
import { FileExplorer } from "@/components/file-explorer/FileExplorer";
import { useUIStore } from "@/store/uiStore";
import { useSandboxStore } from "@/store/sandboxStore";
import { useChatStore } from "@/store/chatStore";
import { useT } from "@/store/settingsStore";

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

export function AppLayout() {
  const rightPanelOpen = useUIStore((s) => s.rightPanelOpen);
  const setRightPanelOpen = useUIStore((s) => s.setRightPanelOpen);
  const mobileTab = useUIStore((s) => s.mobileTab);
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
      fm().then(({ models, profile }) => {
        useChatStore.getState().setModels(models);
        useChatStore.getState().setProfileName(profile);
      });
    });
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
              <ChatArea />
            </Panel>

            <PanelResizeHandle
              className="w-px bg-border hover:bg-primary active:bg-primary transition-colors"
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
            <div className="shrink-0 border-l border-border/50 flex items-start pt-2.5 px-1">
              <RightPanelToggle />
            </div>
          )}
        </div>

        {/* Mobile: tabbed views */}
        <div className="flex md:hidden flex-1 flex-col min-h-0">
          {mobileTab === "chat" && <ChatArea />}
          {mobileTab === "files" && (
            <div className="flex-1 flex flex-col bg-card">
              <div className="flex items-center px-3 py-2.5 border-b border-border/50">
                <MobileMenuButton />
                <h2 className="text-[14px] font-medium text-foreground ml-2">
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
              <div className="flex items-center px-3 py-2.5 border-b border-border/50">
                <MobileMenuButton />
                <h2 className="text-[14px] font-medium text-foreground ml-2">
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
        </div>
      </div>
    </div>
  );
}
