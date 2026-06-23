import { useState } from "react";
import { Brain } from "lucide-react";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import { MemoriesTab } from "@/components/brain/MemoriesTab";
import { SkillsTab } from "@/components/brain/SkillsTab";
import { DocumentsTab } from "@/components/brain/DocumentsTab";
import { HistoryTab } from "@/components/brain/HistoryTab";

type BrainTab = "memories" | "history" | "skills" | "documents";

export function BrainPage() {
  const [tab, setTab] = useState<BrainTab>("memories");
  const t = useT();

  const tabs: { key: BrainTab; label: string }[] = [
    { key: "memories", label: t("brain.memories") },
    { key: "history", label: t("brain.history") },
    { key: "skills", label: t("brain.skills") },
    { key: "documents", label: t("brain.documents") },
  ];

  return (
    <div className="flex h-full w-full flex-col bg-background">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 py-3.5 border-b border-border/50">
        <div className="flex items-center gap-2">
          <Brain className="w-5 h-5 text-primary" />
          <h1 className="text-[16px] font-semibold text-foreground">
            {t("brain.title")}
          </h1>
        </div>
      </div>

      {/* Tab Bar */}
      <div className="flex border-b border-border/50">
        {tabs.map((tabItem) => (
          <button
            key={tabItem.key}
            onClick={() => setTab(tabItem.key)}
            className={cn(
              "flex-1 py-2.5 text-[13px] font-medium transition-colors relative",
              tab === tabItem.key
                ? "text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {tabItem.label}
            {tab === tabItem.key && (
              <div className="absolute bottom-0 left-1/2 -translate-x-1/2 w-12 h-0.5 bg-primary rounded-full" />
            )}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {tab === "memories" && <MemoriesTab />}
        {tab === "history" && <HistoryTab />}
        {tab === "skills" && <SkillsTab />}
        {tab === "documents" && <DocumentsTab />}
      </div>
    </div>
  );
}
