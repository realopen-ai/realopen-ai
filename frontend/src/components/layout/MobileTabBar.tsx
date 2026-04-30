import { MessageSquare, FolderTree, Terminal } from "lucide-react";
import { useUIStore } from "@/store/uiStore";
import { cn } from "@/lib/utils";

export function MobileTabBar() {
  const mobileTab = useUIStore((s) => s.mobileTab);
  const setMobileTab = useUIStore((s) => s.setMobileTab);

  const tabs = [
    { id: "chat" as const, icon: MessageSquare, label: "Chat" },
    { id: "files" as const, icon: FolderTree, label: "Files" },
    { id: "terminal" as const, icon: Terminal, label: "Terminal" },
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
          <tab.icon className="w-[18px] h-[18px]" />
          <span className="text-[10px] font-medium">{tab.label}</span>
        </button>
      ))}
    </div>
  );
}
