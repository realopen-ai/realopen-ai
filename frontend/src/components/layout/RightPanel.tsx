import { lazy, Suspense } from "react";
import {
  PanelRightClose,
  PanelRightOpen,
  FolderTree,
  Terminal as TerminalIcon,
  RefreshCw,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useUIStore } from "@/store/uiStore";
import { useSandboxStore } from "@/store/sandboxStore";
import { FileExplorer } from "@/components/file-explorer/FileExplorer";
import { cn } from "@/lib/utils";

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
  const toggleRightPanel = useUIStore((s) => s.toggleRightPanel);
  const rightPanelTab = useUIStore((s) => s.rightPanelTab);
  const setRightPanelTab = useUIStore((s) => s.setRightPanelTab);
  const fetchFileTree = useSandboxStore((s) => s.fetchFileTree);
  const isLoadingTree = useSandboxStore((s) => s.isLoadingTree);

  return (
    <div className="flex flex-col h-full bg-card">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border/50">
        <Tabs
          value={rightPanelTab}
          onValueChange={(v) => setRightPanelTab(v as "files" | "terminal")}
        >
          <TabsList className="h-7 bg-secondary/50 p-0.5">
            <TabsTrigger
              value="files"
              className="h-6 text-[11px] gap-1 px-2.5 rounded-md data-[state=active]:bg-accent data-[state=active]:shadow-none"
            >
              <FolderTree className="w-3 h-3" />
              Files
            </TabsTrigger>
            <TabsTrigger
              value="terminal"
              className="h-6 text-[11px] gap-1 px-2.5 rounded-md data-[state=active]:bg-accent data-[state=active]:shadow-none"
            >
              <TerminalIcon className="w-3 h-3" />
              Terminal
            </TabsTrigger>
          </TabsList>
        </Tabs>
        <div className="flex items-center gap-0.5">
          {rightPanelTab === "files" && (
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

      {/* Content */}
      <div className="flex-1 overflow-hidden">
        {rightPanelTab === "files" ? (
          <FileExplorer />
        ) : (
          <Suspense fallback={<TerminalLoader />}>
            <TerminalPane />
          </Suspense>
        )}
      </div>
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
