import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  Search,
  MessageSquare,
  Archive,
  ChevronDown,
  ChevronRight,
  PanelLeftClose,
  PanelLeftOpen,
  Menu,
  Settings,
  Brain,
  FolderOpen,
  GraduationCap,
  SquarePen,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { SettingsModal } from "@/components/settings/SettingsModal";
import { ConversationItem } from "@/components/layout/ConversationItem";
import { SearchConversationsModal } from "@/components/layout/SearchConversationsModal";
import { useChatStore } from "@/store/chatStore";
import { useT } from "@/store/settingsStore";
import { useUIStore } from "@/store/uiStore";
import { cn } from "@/lib/utils";
import { isBrainRoute, isWorkspaceRoute, isLearnRoute } from "@/lib/appRoutes";

/* ── Shared row primitives (sidebar-local) ─────────────────────── */

/**
 * Expanded sidebar navigation row: 36px tall, quiet surfaces,
 * restrained selected state (no pills, no bordered boxes).
 */
function NavRow({
  icon: Icon,
  label,
  onClick,
  active = false,
  prominent = false,
  className,
}: {
  icon: LucideIcon;
  label: string;
  onClick?: () => void;
  active?: boolean;
  /** Slightly stronger text color for the primary action (New chat). */
  prominent?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-[13.5px] transition-colors",
        active
          ? "bg-primary/10 font-medium text-primary"
          : prominent
            ? "text-foreground hover:bg-surface-hover"
            : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
        className,
      )}
    >
      <Icon className="size-4 shrink-0" />
      <span className="min-w-0 truncate text-left">{label}</span>
    </button>
  );
}

/**
 * Collapsed rail button: centered icon, same icon size as the expanded
 * sidebar, tooltip, and a quiet selected state with a 2px primary
 * indicator bar on the leading edge.
 */
function RailButton({
  icon: Icon,
  label,
  onClick,
  active = false,
  activeVariant = "surface",
}: {
  icon: LucideIcon;
  label: string;
  onClick?: () => void;
  active?: boolean;
  /** Page nav uses the primary tint; list rows use the selected surface. */
  activeVariant?: "surface" | "primary";
}) {
  return (
    <Tooltip delayDuration={300}>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onClick}
          aria-label={label}
          className={cn(
            "relative flex h-9 w-9 items-center justify-center rounded-lg transition-colors",
            active
              ? activeVariant === "primary"
                ? "bg-primary/10 text-primary"
                : "bg-surface-selected text-foreground"
              : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
          )}
        >
          {active && (
            <span className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full bg-primary" />
          )}
          <Icon className="size-4" />
        </button>
      </TooltipTrigger>
      <TooltipContent side="right" sideOffset={8}>
        {label}
      </TooltipContent>
    </Tooltip>
  );
}

/** Hairline separator — spacing instead of bordered boxes. */
function RailSeparator() {
  return <div className="my-1 h-px w-6 shrink-0 bg-border/60" />;
}

/* ── Sidebar ───────────────────────────────────────────────────── */

export function Sidebar() {
  const conversations = useChatStore((s) => s.conversations);
  const deleteConversation = useChatStore((s) => s.deleteConversation);
  const profileLabel = useChatStore((s) => s.profileLabel);
  const profileName = useChatStore((s) => s.profileName);

  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { conversationId: urlConvId } = useParams<{ conversationId: string }>();

  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);
  const sidebarMobileOpen = useUIStore((s) => s.sidebarMobileOpen);
  const setSidebarMobileOpen = useUIStore((s) => s.setSidebarMobileOpen);
  const showBrainPage = isBrainRoute(pathname);
  const showWorkspacePage = isWorkspaceRoute(pathname);
  const showLearnPage = isLearnRoute(pathname);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [archivedOpen, setArchivedOpen] = useState(false);
  const t = useT();

  // Command+K / Ctrl+K toggles the conversation search modal
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // Split + sort the conversation list for display:
  //   active: pinned first (most recently pinned at top), then by recency
  //   archived: by recency, under a collapsible "Archived" section
  const { activeConversations, archivedConversations } = useMemo(() => {
    const active = conversations
      .filter((c) => !c.archived && !c.isNotebook)
      .sort((a, b) => {
        if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
        if (a.pinned && b.pinned) return (b.pinnedAt ?? 0) - (a.pinnedAt ?? 0);
        return b.updatedAt - a.updatedAt;
      });
    const archived = conversations
      .filter((c) => c.archived && !c.isNotebook)
      .sort((a, b) => b.updatedAt - a.updatedAt);
    return { activeConversations: active, archivedConversations: archived };
  }, [conversations]);

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

  const handleDelete = (id: string) => {
    deleteConversation(id);
    // If we're currently viewing this conversation, go home
    if (id === urlConvId) {
      navigate("/");
    }
  };

  const handleBrainClick = () => {
    navigate("/brain");
    setSidebarMobileOpen(false);
  };

  const handleWorkspaceClick = () => {
    navigate("/workspace");
    setSidebarMobileOpen(false);
  };

  const handleSettingsClick = () => {
    setSettingsOpen(true);
    setSidebarMobileOpen(false);
  };

  const handleOpenSearch = () => {
    setSearchOpen(true);
    // Close the mobile drawer so the modal isn't trapped underneath it
    setSidebarMobileOpen(false);
  };

  const handleOpenFromSearch = (conversationId: string) => {
    navigate(`/${conversationId}`);
    setSidebarMobileOpen(false);
    setSearchOpen(false);
  };

  /* The sidebar body is rendered from a single builder so the desktop
     rail and the mobile drawer stay in sync. The mobile drawer always
     renders the expanded layout (it has plenty of width). */
  const sidebarContent = (collapsed: boolean) =>
    collapsed ? (
      // ── Collapsed icon rail ──────────────────────────────────────
      <div className="flex h-full flex-col items-center bg-sidebar-bg">
        {/* Header: search */}
        <div className="flex h-10 w-full shrink-0 items-center justify-center pb-1 pt-3">
          <RailButton
            icon={Search}
            label={t("sidebar.search")}
            onClick={handleOpenSearch}
          />
        </div>

        {/* Primary navigation */}
        <nav
          className="flex shrink-0 flex-col items-center gap-0.5 pb-1.5"
          aria-label={t("sidebar.chats")}
        >
          <RailButton
            icon={SquarePen}
            label={t("sidebar.newChat")}
            onClick={handleNewChat}
          />
          <RailButton
            icon={Brain}
            label={t("brain.title")}
            onClick={handleBrainClick}
            active={showBrainPage}
            activeVariant="primary"
          />
          <RailButton
            icon={FolderOpen}
            label={t("workspace.title")}
            onClick={handleWorkspaceClick}
            active={showWorkspacePage}
            activeVariant="primary"
          />
          <RailButton
            icon={GraduationCap}
            label={t("learn.title")}
            active={showLearnPage}
            activeVariant="primary"
            onClick={() => {
              navigate("/learn");
              setSidebarMobileOpen(false);
            }}
          />
        </nav>

        <RailSeparator />

        {/* Conversation list */}
        <ScrollArea className="min-h-0 w-full flex-1">
          <div className="flex flex-col items-center gap-0.5 py-1">
            {activeConversations.map((conv) => (
              <ConversationItem
                key={conv.id}
                conv={conv}
                active={conv.id === urlConvId}
                collapsed
                onSelect={handleSelect}
                onDelete={handleDelete}
              />
            ))}
          </div>
        </ScrollArea>

        {/* Footer: settings · profile · expand */}
        <div className="mt-1 flex w-full shrink-0 flex-col items-center gap-0.5 border-t border-border/60 px-1 pb-2 pt-1.5">
          <RailButton
            icon={Settings}
            label={t("sidebar.settings")}
            onClick={handleSettingsClick}
          />

          <Tooltip delayDuration={300}>
            <TooltipTrigger asChild>
              <div className="flex h-9 w-9 items-center justify-center">
                <span className="size-1.5 rounded-full bg-success" />
              </div>
            </TooltipTrigger>
            <TooltipContent
              side="right"
              sideOffset={8}
              className="max-w-52 truncate"
            >
              {profileLabel || profileName || "—"}
            </TooltipContent>
          </Tooltip>

          {/* Expand toggle (desktop only) */}
          <div className="hidden md:block">
            <RailButton
              icon={PanelLeftOpen}
              label={t("sidebar.collapse")}
              onClick={toggleSidebar}
            />
          </div>
        </div>
      </div>
    ) : (
      // ── Expanded sidebar ─────────────────────────────────────────
      <div className="flex h-full flex-col bg-sidebar-bg">
        {/* Header */}
        {/* sidebar-chats-row: inside the mobile drawer, extra right padding keeps
            the search icon clear of the Sheet's built-in close (X) button */}
        <div className="shrink-0 px-2 pb-1 pt-3">
          <div className="sidebar-chats-row flex h-10 items-center justify-between pl-1.5 pr-1">
            <span className="text-[15px] font-semibold tracking-[-0.01em] text-foreground">
              {t("sidebar.chats")}
            </span>
            <Button
              onClick={handleOpenSearch}
              variant="ghost"
              size="icon"
              aria-label={t("sidebar.search")}
              className="h-7 w-7 rounded-lg"
            >
              <Search className="size-4" />
            </Button>
          </div>
        </div>

        {/* Primary navigation */}
        <nav
          className="shrink-0 space-y-0.5 px-2 pb-1.5"
          aria-label={t("sidebar.chats")}
        >
          <NavRow
            icon={SquarePen}
            label={t("sidebar.newChat")}
            onClick={handleNewChat}
            prominent
          />
          <NavRow
            icon={Brain}
            label={t("brain.title")}
            onClick={handleBrainClick}
            active={showBrainPage}
          />
          <NavRow
            icon={FolderOpen}
            label={t("workspace.title")}
            onClick={handleWorkspaceClick}
            active={showWorkspacePage}
          />
          <NavRow
            icon={GraduationCap}
            label={t("learn.title")}
            active={showLearnPage}
            onClick={() => {
              navigate("/learn");
              setSidebarMobileOpen(false);
            }}
          />
        </nav>

        {/* Hairline between navigation and history */}
        <div className="mx-2 my-1 h-px shrink-0 bg-border/60" />

        {/* Conversation list */}
        <ScrollArea className="min-h-0 flex-1 px-2">
          <div className="space-y-0.5 py-1 pb-2">
            {activeConversations.length === 0 && (
              <div className="flex flex-col items-center justify-center py-12 text-center">
                <MessageSquare className="mb-2 size-5 text-muted-foreground/70" />
                <p className="text-[12.5px] text-muted-foreground/70">
                  {t("sidebar.noConversations")}
                </p>
                <p className="mt-0.5 text-[11.5px] text-muted-foreground/70">
                  {t("sidebar.startNew")}
                </p>
              </div>
            )}
            {activeConversations.map((conv) => (
              <ConversationItem
                key={conv.id}
                conv={conv}
                active={conv.id === urlConvId}
                collapsed={false}
                onSelect={handleSelect}
                onDelete={handleDelete}
              />
            ))}

            {/* Archived conversations (collapsible) */}
            {archivedConversations.length > 0 && (
              <div className="pt-2">
                <button
                  onClick={() => setArchivedOpen((o) => !o)}
                  aria-expanded={archivedOpen}
                  className="flex h-7 w-full items-center gap-1.5 rounded-lg px-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/70 transition-colors hover:bg-surface-hover hover:text-foreground"
                >
                  {archivedOpen ? (
                    <ChevronDown className="size-3.5 shrink-0" />
                  ) : (
                    <ChevronRight className="size-3.5 shrink-0" />
                  )}
                  <Archive className="size-3.5 shrink-0" />
                  <span className="truncate">
                    {t("sidebar.archived")} ({archivedConversations.length})
                  </span>
                </button>
                {archivedOpen && (
                  <div className="mt-0.5 space-y-0.5">
                    {archivedConversations.map((conv) => (
                      <ConversationItem
                        key={conv.id}
                        conv={conv}
                        active={conv.id === urlConvId}
                        collapsed={false}
                        onSelect={handleSelect}
                        onDelete={handleDelete}
                      />
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </ScrollArea>

        {/* Footer: settings · hardware profile · collapse */}
        <div className="mt-1 shrink-0 border-t border-border/60 px-2 pb-2 pt-1.5">
          <NavRow
            icon={Settings}
            label={t("sidebar.settings")}
            onClick={handleSettingsClick}
          />

          {/* Hardware profile */}
          <div className="px-2.5 pb-1 pt-2">
            <div className="flex items-center gap-2">
              <span className="size-1.5 shrink-0 rounded-full bg-success" />
              <span className="min-w-0 truncate text-[12.5px] font-medium text-foreground">
                {profileLabel || profileName || "—"}
              </span>
            </div>
            <p className="mt-0.5 pl-3.5 text-[11px] text-muted-foreground">
              {t("sidebar.hardwareProfile")}
            </p>
            <p className="pl-3.5 text-[10.5px] text-muted-foreground/80">
              {t("sidebar.offlinePrivate")}
            </p>
          </div>

          {/* Collapse toggle (desktop only) */}
          <div className="hidden md:block">
            <button
              type="button"
              onClick={toggleSidebar}
              className="flex h-8 w-full items-center gap-2.5 rounded-lg px-2.5 text-[13px] text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground"
            >
              <PanelLeftClose className="size-4 shrink-0" />
              <span>{t("sidebar.collapse")}</span>
            </button>
          </div>
        </div>
      </div>
    );

  return (
    <>
      {/* Desktop: persistent rail */}
      <aside
        className={cn(
          "hidden md:flex flex-col border-r border-sidebar-border transition-[width] duration-200 ease-out",
          sidebarCollapsed ? "w-13" : "w-65",
        )}
      >
        {sidebarContent(sidebarCollapsed)}
      </aside>

      {/* Mobile: drawer (always the expanded layout) */}
      <Sheet open={sidebarMobileOpen} onOpenChange={setSidebarMobileOpen}>
        <SheetContent
          side="left"
          className="sidebar-mobile-sheet w-70 p-0 border-sidebar-border bg-sidebar-bg"
        >
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          {sidebarContent(false)}
        </SheetContent>
      </Sheet>

      {/* Settings Modal */}
      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />

      {/* Search Conversations Modal */}
      <SearchConversationsModal
        open={searchOpen}
        onClose={() => setSearchOpen(false)}
        onOpenConversation={handleOpenFromSearch}
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
      aria-label="Open menu"
      className="md:hidden text-muted-foreground hover:text-foreground"
      onClick={() => setSidebarMobileOpen(true)}
    >
      <Menu className="size-5" />
    </Button>
  );
}
