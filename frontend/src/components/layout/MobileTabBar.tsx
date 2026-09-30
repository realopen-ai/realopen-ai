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
    <nav className="flex items-stretch border-t border-border/60 bg-background">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => setMobileTab(tab.id)}
          aria-current={mobileTab === tab.id}
          className={cn(
            "mobile-tab relative flex flex-1 flex-col items-center gap-0.5 py-1.5 transition-colors",
            mobileTab === tab.id
              ? "text-foreground active"
              : "text-muted-foreground/80 hover:text-muted-foreground",
          )}
        >
          <tab.icon className="size-4.5" />
          <span className="text-[11px] font-medium">{tab.label}</span>
        </button>
      ))}
    </nav>
  );
}
