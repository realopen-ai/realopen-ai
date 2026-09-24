import { useState } from "react";
import {
  FolderOpen,
  FileText,
  FileImage,
  Package,
  ChevronLeft,
  Files,
  Box,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { TemplatesSection } from "@/components/workspace/TemplatesSection";
import { GeneratedFilesSection } from "@/components/workspace/GeneratedFilesSection";
import { AssetsSection } from "@/components/workspace/AssetsSection";
import { DocumentsSection } from "@/components/workspace/DocumentsSection";
import { useNavigate } from "react-router-dom";
import { useUIStore } from "@/store/uiStore";
import { SandboxesSection } from "@/components/workspace/SandboxesSection";

type WorkspaceView =
  | "folders"
  | "documents"
  | "templates"
  | "generated"
  | "assets"
  | "sandboxes";

const folders = [
  {
    key: "sandboxes" as const,
    label: "Sandboxes",
    icon: Box,
    color: "text-cyan-400",
    bg: "bg-cyan-500/10",
    desc: "Persistent coding workspaces",
  },
  {
    key: "documents" as const,
    label: "Documents",
    icon: Files,
    color: "text-emerald-400",
    bg: "bg-emerald-500/10",
    desc: "Knowledge base files indexed for RAG",
  },
  {
    key: "templates" as const,
    label: "Templates",
    icon: FileText,
    color: "text-amber-400",
    bg: "bg-amber-500/10",
    desc: "PPTX presentation templates",
  },
  {
    key: "generated" as const,
    label: "Generated Files",
    icon: FileImage,
    color: "text-blue-400",
    bg: "bg-blue-500/10",
    desc: "Reports, presentations, and images",
  },
  {
    key: "assets" as const,
    label: "Assets",
    icon: Package,
    color: "text-purple-400",
    bg: "bg-purple-500/10",
    desc: "Logos, branding, and reusable resources",
  },
];

export function WorkspacePage() {
  const [view, setView] = useState<WorkspaceView>("folders");
  const navigate = useNavigate();
  const setShowWorkspacePage = useUIStore((s) => s.setShowWorkspacePage);

  const handleOpenConversation = (conversationId: string) => {
    navigate(`/${conversationId}`);
    setShowWorkspacePage(false);
  };

  if (view === "sandboxes") {
    return (
      <div className="flex h-full w-full flex-col bg-background">
        <div className="flex items-center gap-3 px-5 py-3.5 border-b border-border/50">
          <button
            onClick={() => setView("folders")}
            className="p-1 rounded hover:bg-accent transition-colors"
          >
            <ChevronLeft className="w-5 h-5 text-muted-foreground" />
          </button>
          <Box className="w-5 h-5 text-primary" />
          <h1 className="text-[16px] font-semibold">Workspace · Sandboxes</h1>
        </div>
        <div className="flex-1 min-h-0 overflow-hidden">
          <SandboxesSection />
        </div>
      </div>
    );
  }

  if (view === "documents") {
    return (
      <div className="flex h-full w-full flex-col bg-background">
        <div className="flex items-center gap-3 px-5 py-3.5 border-b border-border/50">
          <button
            onClick={() => setView("folders")}
            className="p-1 rounded hover:bg-accent transition-colors"
          >
            <ChevronLeft className="w-5 h-5 text-muted-foreground" />
          </button>
          <FolderOpen className="w-5 h-5 text-primary" />
          <h1 className="text-[16px] font-semibold text-foreground">
            Workspace · Documents
          </h1>
        </div>
        <div className="flex-1 min-h-0 overflow-hidden">
          <DocumentsSection />
        </div>
      </div>
    );
  }

  if (view === "templates") {
    return (
      <div className="flex h-full w-full flex-col bg-background">
        <div className="flex items-center gap-3 px-5 py-3.5 border-b border-border/50">
          <button
            onClick={() => setView("folders")}
            className="p-1 rounded hover:bg-accent transition-colors"
          >
            <ChevronLeft className="w-5 h-5 text-muted-foreground" />
          </button>
          <FolderOpen className="w-5 h-5 text-primary" />
          <h1 className="text-[16px] font-semibold text-foreground">
            Workspace · Templates
          </h1>
        </div>
        <div className="flex-1 min-h-0 overflow-hidden">
          <TemplatesSection />
        </div>
      </div>
    );
  }

  if (view === "generated") {
    return (
      <div className="flex h-full w-full flex-col bg-background">
        <div className="flex items-center gap-3 px-5 py-3.5 border-b border-border/50">
          <button
            onClick={() => setView("folders")}
            className="p-1 rounded hover:bg-accent transition-colors"
          >
            <ChevronLeft className="w-5 h-5 text-muted-foreground" />
          </button>
          <FolderOpen className="w-5 h-5 text-primary" />
          <h1 className="text-[16px] font-semibold text-foreground">
            Workspace · Generated Files
          </h1>
        </div>
        <div className="flex-1 min-h-0 overflow-hidden">
          <GeneratedFilesSection onOpenConversation={handleOpenConversation} />
        </div>
      </div>
    );
  }

  if (view === "assets") {
    return (
      <div className="flex h-full w-full flex-col bg-background">
        <div className="flex items-center gap-3 px-5 py-3.5 border-b border-border/50">
          <button
            onClick={() => setView("folders")}
            className="p-1 rounded hover:bg-accent transition-colors"
          >
            <ChevronLeft className="w-5 h-5 text-muted-foreground" />
          </button>
          <FolderOpen className="w-5 h-5 text-primary" />
          <h1 className="text-[16px] font-semibold text-foreground">
            Workspace · Assets
          </h1>
        </div>
        <div className="flex-1 min-h-0 overflow-hidden">
          <AssetsSection />
        </div>
      </div>
    );
  }

  // Folder view — icon grid
  return (
    <div className="flex h-full w-full flex-col bg-background">
      <div className="flex items-center gap-3 px-5 py-3.5 border-b border-border/50">
        <FolderOpen className="w-5 h-5 text-primary" />
        <h1 className="text-[16px] font-semibold text-foreground">Workspace</h1>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-6">
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4 max-w-5xl">
          {folders.map((folder) => {
            const Icon = folder.icon;
            return (
              <button
                key={folder.key}
                onClick={() => setView(folder.key)}
                className="flex flex-col items-center gap-3 p-5 rounded-2xl border border-border hover:border-primary/30 hover:bg-accent/30 transition-all group"
              >
                <div
                  className={cn(
                    "w-16 h-16 rounded-2xl flex items-center justify-center",
                    folder.bg,
                  )}
                >
                  <Icon className={cn("w-8 h-8", folder.color)} />
                </div>
                <div className="text-center">
                  <p className="text-[13px] font-medium text-foreground">
                    {folder.label}
                  </p>
                  <p className="text-[11px] text-muted-foreground/60 mt-0.5">
                    {folder.desc}
                  </p>
                </div>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
