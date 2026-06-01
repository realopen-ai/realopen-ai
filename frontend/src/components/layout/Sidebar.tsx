import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Plus,
  MessageSquare,
  Trash2,
  PanelLeftClose,
  PanelLeftOpen,
  Menu,
  Settings,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { SettingsModal } from "@/components/settings/SettingsModal";
import { useChatStore } from "@/store/chatStore";
import { useT } from "@/store/settingsStore";
import { useUIStore } from "@/store/uiStore";
import { cn } from "@/lib/utils";

export function Sidebar() {
  const conversations = useChatStore((s) => s.conversations);
  const deleteConversation = useChatStore((s) => s.deleteConversation);
  const profileLabel = useChatStore((s) => s.profileLabel);
  const profileName = useChatStore((s) => s.profileName);

  const navigate = useNavigate();
  const { conversationId: urlConvId } = useParams<{ conversationId: string }>();

  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);
  const sidebarMobileOpen = useUIStore((s) => s.sidebarMobileOpen);
  const setSidebarMobileOpen = useUIStore((s) => s.setSidebarMobileOpen);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const t = useT();

  const handleNewChat = () => {
    // Navigate to home page — the user will start a new conversation
    // by sending a message from the welcome screen
    navigate("/");
    setSidebarMobileOpen(false);
  };

  const handleSelect = (id: string) => {
    // Navigate to the conversation URL
    navigate(`/${id}`);
    setSidebarMobileOpen(false);
  };

  const handleDelete = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    deleteConversation(id);
    // If we're currently viewing this conversation, go home
    if (id === urlConvId) {
      navigate("/");
    }
  };

  const sidebarContent = (
    <div className="flex flex-col h-full bg-sidebar-bg">
      {/* Header */}
      <div className="px-3 pt-4 pb-2">
        {!sidebarCollapsed ? (
          <div className="flex items-center justify-between">
            <span className="text-[15px] font-semibold text-foreground">
              {t("sidebar.chats")}
            </span>
            <Button
              onClick={handleNewChat}
              variant="ghost"
              size="icon"
              className="h-7 w-7 text-muted-foreground hover:text-foreground hover:bg-sidebar-accent rounded-lg"
            >
              <Plus className="w-4 h-4" />
            </Button>
          </div>
        ) : (
          <div className="flex justify-center">
            <Button
              onClick={handleNewChat}
              variant="ghost"
              size="icon"
              className="h-7 w-7 text-muted-foreground hover:text-foreground hover:bg-sidebar-accent rounded-lg"
            >
              <Plus className="w-4 h-4" />
            </Button>
          </div>
        )}
      </div>

      {/* New Chat Button */}
      {!sidebarCollapsed && (
        <div className="px-2 pb-2">
          <button
            onClick={handleNewChat}
            className="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-[13px] text-foreground bg-sidebar-accent hover:bg-accent transition-colors"
          >
            <Plus className="w-4 h-4" />
            {t("sidebar.newChat")}
          </button>
        </div>
      )}

      {/* Conversation List */}
      <ScrollArea className="flex-1 px-2">
        <div className="space-y-0.5 pb-2">
          {conversations.length === 0 && !sidebarCollapsed && (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <MessageSquare className="w-6 h-6 text-muted-foreground/40 mb-2" />
              <p className="text-[12px] text-muted-foreground/60">
                {t("sidebar.noConversations")}
              </p>
              <p className="text-[11px] text-muted-foreground/40">
                {t("sidebar.startNew")}
              </p>
            </div>
          )}
          {conversations.map((conv) => (
            <div
              key={conv.id}
              onClick={() => handleSelect(conv.id)}
              className={cn(
                "group flex items-center gap-2 px-3 py-2 rounded-xl cursor-pointer transition-colors",
                conv.id === urlConvId
                  ? "bg-sidebar-accent text-foreground"
                  : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground",
                sidebarCollapsed && "justify-center px-0",
              )}
            >
              <MessageSquare className="w-4 h-4 shrink-0 opacity-50" />
              {!sidebarCollapsed && (
                <>
                  <span className="text-[13px] truncate flex-1">
                    {conv.title}
                  </span>
                  <button
                    onClick={(e) => handleDelete(e, conv.id)}
                    className="opacity-0 group-hover:opacity-100 p-0.5 hover:text-destructive transition-all"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </>
              )}
            </div>
          ))}
        </div>
      </ScrollArea>

      {/* Footer - Hardware Profile + Settings */}
      <div className="border-t border-sidebar-border">
        {/* Settings Button */}
        <div className="px-2 py-1.5">
          <button
            onClick={() => {
              setSettingsOpen(true);
              setSidebarMobileOpen(false);
            }}
            className={cn(
              "w-full flex items-center gap-2 px-3 py-2 rounded-xl text-[13px] text-muted-foreground hover:text-foreground hover:bg-sidebar-accent transition-colors",
              sidebarCollapsed && "justify-center px-0",
            )}
          >
            <Settings className="w-4 h-4 shrink-0" />
            {!sidebarCollapsed && <span>{t("sidebar.settings")}</span>}
          </button>
        </div>

        {/* Hardware Profile */}
        {!sidebarCollapsed && (
          <div className="px-3 py-2.5 border-t border-sidebar-border">
            <div className="flex items-center gap-2">
              <div className="w-2 h-2 rounded-full bg-emerald-500" />
              <span className="text-[13px] text-foreground font-medium">
                {profileLabel || profileName || "—"}
              </span>
            </div>
            <p className="text-[11px] text-muted-foreground mt-0.5 ml-4">
              {t("sidebar.hardwareProfile")}
            </p>
            <p className="text-[10px] text-muted-foreground/50 mt-1 ml-4">
              {t("sidebar.offlinePrivate")}
            </p>
          </div>
        )}
        {sidebarCollapsed && (
          <div className="flex justify-center py-2.5 border-t border-sidebar-border">
            <div
              className="w-2 h-2 rounded-full bg-emerald-500"
              title={profileLabel || profileName}
            />
          </div>
        )}

        {/* Collapse Toggle (desktop only) */}
        <div className="hidden md:block px-2 py-1.5 border-t border-sidebar-border">
          <Button
            onClick={toggleSidebar}
            variant="ghost"
            size="sm"
            className={cn(
              "w-full text-muted-foreground hover:text-foreground hover:bg-transparent",
              !sidebarCollapsed ? "justify-start gap-2" : "justify-center",
            )}
          >
            {sidebarCollapsed ? (
              <PanelLeftOpen className="w-4 h-4" />
            ) : (
              <>
                <PanelLeftClose className="w-4 h-4" />
                <span className="text-[12px]">{t("sidebar.collapse")}</span>
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  );

  return (
    <>
      {/* Desktop: Persistent */}
      <aside
        className={cn(
          "hidden md:flex flex-col border-r border-sidebar-border transition-[width] duration-100 ease-in-out",
          sidebarCollapsed ? "w-13" : "w-60",
        )}
      >
        {sidebarContent}
      </aside>

      {/* Mobile: Drawer */}
      <Sheet open={sidebarMobileOpen} onOpenChange={setSidebarMobileOpen}>
        <SheetContent
          side="left"
          className="w-70 p-0 border-sidebar-border bg-sidebar-bg"
        >
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          {sidebarContent}
        </SheetContent>
      </Sheet>

      {/* Settings Modal */}
      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
    </>
  );
}

export function MobileMenuButton() {
  const setSidebarMobileOpen = useUIStore((s) => s.setSidebarMobileOpen);
  return (
    <Button
      variant="ghost"
      size="icon"
      className="md:hidden text-muted-foreground hover:text-foreground"
      onClick={() => setSidebarMobileOpen(true)}
    >
      <Menu className="w-5 h-5" />
    </Button>
  );
}
