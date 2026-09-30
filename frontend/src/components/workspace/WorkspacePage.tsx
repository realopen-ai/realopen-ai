import { type ReactNode } from "react";
import {
  Box,
  ChevronLeft,
  ChevronRight,
  FileImage,
  FileText,
  Files,
  Package,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useLocation, useNavigate } from "react-router-dom";
import { PageContainer, PageHeader } from "@/components/ui/primitives";
import { TemplatesSection } from "@/components/workspace/TemplatesSection";
import { GeneratedFilesSection } from "@/components/workspace/GeneratedFilesSection";
import { AssetsSection } from "@/components/workspace/AssetsSection";
import { DocumentsSection } from "@/components/workspace/DocumentsSection";
import { SandboxesSection } from "@/components/workspace/SandboxesSection";
import { useT } from "@/store/settingsStore";
import {
  getWorkspaceSection,
  workspaceSectionPath,
  type WorkspaceSection,
} from "@/lib/appRoutes";

type SectionView = WorkspaceSection;

const folders: {
  key: SectionView;
  labelKey: string;
  icon: typeof Box;
  iconClass: string;
  descKey: string;
}[] = [
  {
    key: "sandboxes",
    labelKey: "workspace.sandboxes",
    icon: Box,
    iconClass: "text-cyan-600 dark:text-cyan-400",
    descKey: "workspace.sandboxes.shortDescription",
  },
  {
    key: "documents",
    labelKey: "workspace.documents",
    icon: Files,
    iconClass: "text-emerald-600 dark:text-emerald-400",
    descKey: "workspace.documents.shortDescription",
  },
  {
    key: "templates",
    labelKey: "workspace.templates",
    icon: FileText,
    iconClass: "text-amber-600 dark:text-amber-400",
    descKey: "workspace.templates.shortDescription",
  },
  {
    key: "generated",
    labelKey: "workspace.generated",
    icon: FileImage,
    iconClass: "text-blue-600 dark:text-blue-400",
    descKey: "workspace.generated.shortDescription",
  },
  {
    key: "assets",
    labelKey: "workspace.assets",
    icon: Package,
    iconClass: "text-purple-600 dark:text-purple-400",
    descKey: "workspace.assets.shortDescription",
  },
];

const sectionMeta: Record<SectionView, { titleKey: string; descriptionKey: string }> =
  {
    sandboxes: {
      titleKey: "workspace.sandboxes",
      descriptionKey: "workspace.sandboxes.description",
    },
    documents: {
      titleKey: "workspace.documents",
      descriptionKey: "workspace.documents.description",
    },
    templates: {
      titleKey: "workspace.templates",
      descriptionKey: "workspace.templates.description",
    },
    generated: {
      titleKey: "workspace.generated",
      descriptionKey: "workspace.generated.description",
    },
    assets: {
      titleKey: "workspace.assets",
      descriptionKey: "workspace.assets.description",
    },
  };

/** Chrome shared by every workspace sub-section: back link + page header,
 *  with the section filling the remaining height. */
function SectionShell({
  view,
  onBack,
  children,
}: {
  view: SectionView;
  onBack: () => void;
  children: ReactNode;
}) {
  const meta = sectionMeta[view];
  const t = useT();
  return (
    <div className="flex h-full w-full flex-col bg-background">
      <div className="shrink-0 px-6 pt-7 lg:px-10 lg:pt-8">
        <div className="mx-auto flex w-full max-w-300 items-start gap-2">
          <button
            onClick={onBack}
            aria-label={t("workspace.back")}
            className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          >
            <ChevronLeft className="h-5 w-5" />
          </button>
          <div className="min-w-0">
            <h1 className="text-[22px] font-semibold leading-tight tracking-[-0.01em] text-foreground">
              {t(meta.titleKey)}
            </h1>
            <p className="mt-1.5 text-[14px] leading-relaxed text-muted-foreground">
              {t(meta.descriptionKey)}
            </p>
          </div>
        </div>
      </div>
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  );
}

export function WorkspacePage() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const view = getWorkspaceSection(pathname);
  const t = useT();

  const handleOpenConversation = (conversationId: string) => {
    navigate(`/${conversationId}`);
  };

  if (view === "sandboxes") {
    return (
      <SectionShell view="sandboxes" onBack={() => navigate("/workspace")}>
        <SandboxesSection />
      </SectionShell>
    );
  }

  if (view === "documents") {
    return (
      <SectionShell view="documents" onBack={() => navigate("/workspace")}>
        <DocumentsSection />
      </SectionShell>
    );
  }

  if (view === "templates") {
    return (
      <SectionShell view="templates" onBack={() => navigate("/workspace")}>
        <TemplatesSection />
      </SectionShell>
    );
  }

  if (view === "generated") {
    return (
      <SectionShell view="generated" onBack={() => navigate("/workspace")}>
        <GeneratedFilesSection onOpenConversation={handleOpenConversation} />
      </SectionShell>
    );
  }

  if (view === "assets") {
    return (
      <SectionShell view="assets" onBack={() => navigate("/workspace")}>
        <AssetsSection />
      </SectionShell>
    );
  }

  // Folder view — workspace tiles
  return (
    <div className="h-full w-full overflow-y-auto bg-background">
      <PageContainer width="wide" className="pb-16">
        <PageHeader
          title={t("workspace.title")}
          description={t("workspace.description")}
          className="mb-8"
        />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {folders.map((folder) => {
            const Icon = folder.icon;
            return (
              <button
                key={folder.key}
                onClick={() => navigate(workspaceSectionPath(folder.key))}
                className="group flex items-center gap-4 rounded-xl border border-border/60 bg-card p-5 text-left transition-all hover:border-border hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              >
                <div
                  className={cn(
                    "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-secondary [&_svg]:size-5",
                    folder.iconClass,
                  )}
                >
                  <Icon />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-[15px] font-medium text-foreground">
                    {t(folder.labelKey)}
                  </p>
                  <p className="mt-0.5 text-[13px] leading-relaxed text-muted-foreground">
                    {t(folder.descKey)}
                  </p>
                </div>
                <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground/70 transition-all group-hover:translate-x-0.5 group-hover:text-muted-foreground" />
              </button>
            );
          })}
        </div>
      </PageContainer>
    </div>
  );
}
