import { MessageSquare, FolderTree, Terminal } from "lucide-react";
import { useUIStore } from "@/store/uiStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";

export function MobileTabBar() {
  const mobileTab = useUIStore((s) => s.mobileTab);
  const setMobileTab = useUIStore((s) => s.setMobileTab);
  const t = useT();

  const tabs = [
    { id: "chat" as const, icon: MessageSquare, label: t("mobile.chat") },
    { id: "files" as const, icon: FolderTree, label: t("mobile.files") },
    { id: "terminal" as const, icon: Terminal, label: t("mobile.terminal") },
  ];

  return (
    <div className="flex items-center justify-around border-t border-border/50 bg-background px-2 py-1">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => setMobileTab(tab.id)}
          className={cn(
            "mobile-tab relative flex flex-col items-center gap-0.5 px-5 py-1.5 rounded-lg transition-colors",
            mobileTab === tab.id
              ? "text-foreground active"
              : "text-muted-foreground/50 hover:text-muted-foreground",
          )}
        >
          <tab.icon className="w-4.5 h-4.5" />
          <span className="text-[10px] font-medium">{tab.label}</span>
        </button>
      ))}
    </div>
  );
}
