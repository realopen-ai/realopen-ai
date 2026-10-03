import { useLocation, useNavigate } from "react-router-dom";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import { PageContainer, PageHeader } from "@/components/ui/primitives";
import { MemoriesTab } from "@/components/brain/MemoriesTab";
import { SkillsTab } from "@/components/brain/SkillsTab";
import { HistoryTab } from "@/components/brain/HistoryTab";
import { ToolsTab } from "@/components/brain/ToolsTab";
import { brainTabPath, getBrainRoute, type BrainTab } from "@/lib/appRoutes";

export function BrainPage() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { tab, toolId } = getBrainRoute(pathname);
  const t = useT();

  const tabs: { key: BrainTab; label: string }[] = [
    { key: "memories", label: t("brain.memories") },
    { key: "history", label: t("brain.history") },
    { key: "skills", label: t("brain.skills") },
    { key: "tools", label: t("brain.tools") },
  ];

  return (
    <div className="h-full w-full overflow-y-auto bg-background">
      <PageContainer width="list" className="pb-16">
        <PageHeader
          title={t("brain.title")}
          description={t("brain.description")}
          className="mb-6"
        />

        {/* Compact tab navigation — near the content start, not
            stretched across the page. */}
        <nav
          aria-label={t("brain.title")}
          className="flex items-center gap-1 border-b border-border/60"
        >
          {tabs.map((tabItem) => {
            const active = tab === tabItem.key;
            return (
              <button
                key={tabItem.key}
                onClick={() => navigate(brainTabPath(tabItem.key))}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "relative px-3.5 py-2.5 text-[13.5px] font-medium transition-colors rounded-t-md",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
                  active
                    ? "text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {tabItem.label}
                {active && (
                  <span className="absolute inset-x-2.5 -bottom-px h-0.5 rounded-full bg-primary" />
                )}
              </button>
            );
          })}
        </nav>

        {/* Consistent content container across all tabs */}
        <div className="pt-7">
          {tab === "memories" && <MemoriesTab />}
          {tab === "history" && (
            <HistoryTab onOpenConversation={(id) => navigate(`/${id}`)} />
          )}
          {tab === "skills" && <SkillsTab />}
          {tab === "tools" && (
            <ToolsTab
              selectedTool={toolId}
              onSelectTool={(id) =>
                navigate(
                  id
                    ? `/brain/tools/${encodeURIComponent(id)}`
                    : "/brain/tools",
                )
              }
            />
          )}
        </div>
      </PageContainer>
    </div>
  );
}
