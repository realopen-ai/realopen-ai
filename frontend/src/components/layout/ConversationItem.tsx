import { useEffect, useRef, useState } from "react";
import {
  Archive,
  ArchiveRestore,
  MessageSquare,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Trash2,
} from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useChatStore, type Conversation } from "@/store/chatStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";

interface ConversationItemProps {
  conv: Conversation;
  active: boolean;
  collapsed: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
}

/**
 * A single conversation row in the sidebar.
 *
 * Hovering the row reveals a three-dots button that opens a dropdown menu
 * with: Rename (inline edit), Pin chat, Archive and Delete (two-step
 * confirm). Pinned conversations show a pin icon instead of the chat
 * bubble.
 */
export function ConversationItem({
  conv,
  active,
  collapsed,
  onSelect,
  onDelete,
}: ConversationItemProps) {
  const t = useT();
  const renameConversation = useChatStore((s) => s.renameConversation);
  const togglePinConversation = useChatStore((s) => s.togglePinConversation);
  const toggleArchiveConversation = useChatStore(
    (s) => s.toggleArchiveConversation,
  );

  const [renaming, setRenaming] = useState(false);
  const [draftTitle, setDraftTitle] = useState(conv.title);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Focus + select the whole title when entering rename mode
  useEffect(() => {
    if (renaming) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [renaming]);

  const startRename = () => {
    setDraftTitle(conv.title);
    setRenaming(true);
  };

  const commitRename = () => {
    if (!renaming) return;
    setRenaming(false);
    const clean = draftTitle.trim();
    if (clean && clean !== conv.title) {
      renameConversation(conv.id, clean);
    }
  };

  const cancelRename = () => {
    setRenaming(false);
    setDraftTitle(conv.title);
  };

  return (
    <div
      onClick={() => !renaming && onSelect(conv.id)}
      className={cn(
        "group flex items-center gap-2 px-3 py-2 rounded-xl cursor-pointer transition-colors",
        active
          ? "bg-sidebar-accent text-foreground"
          : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground",
        collapsed && "justify-center px-0",
      )}
    >
      {conv.pinned ? (
        <Pin className="w-4 h-4 shrink-0 text-primary fill-primary/20" />
      ) : (
        <MessageSquare className="w-4 h-4 shrink-0 opacity-50" />
      )}

      {!collapsed &&
        (renaming ? (
          <input
            ref={inputRef}
            value={draftTitle}
            onChange={(e) => setDraftTitle(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onBlur={commitRename}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Enter") {
                commitRename();
              } else if (e.key === "Escape") {
                cancelRename();
              }
            }}
            className="flex-1 min-w-0 bg-background border border-primary/50 rounded-md px-1.5 py-0.5 text-[13px] text-foreground outline-none focus:ring-1 focus:ring-primary/40"
            aria-label={t("sidebar.rename")}
            maxLength={120}
          />
        ) : (
          <>
            <span className="text-[13px] truncate flex-1" title={conv.title}>
              {conv.title}
            </span>

            <DropdownMenu
              onOpenChange={(open) => {
                // Reset the two-step delete confirm when the menu closes
                if (!open) setConfirmDelete(false);
              }}
            >
              <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
                <button
                  aria-label={t("sidebar.options")}
                  className="opacity-0 group-hover:opacity-100 data-[state=open]:opacity-100 p-0.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent transition-all focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                >
                  <MoreHorizontal className="w-4 h-4" />
                </button>
              </DropdownMenuTrigger>

              <DropdownMenuContent
                align="end"
                onClick={(e) => e.stopPropagation()}
              >
                <DropdownMenuItem onSelect={() => startRename()}>
                  <Pencil />
                  {t("sidebar.rename")}
                </DropdownMenuItem>

                <DropdownMenuItem
                  onSelect={() => togglePinConversation(conv.id)}
                >
                  {conv.pinned ? <PinOff /> : <Pin />}
                  {conv.pinned ? t("sidebar.unpinChat") : t("sidebar.pinChat")}
                </DropdownMenuItem>

                <DropdownMenuItem
                  onSelect={() => toggleArchiveConversation(conv.id)}
                >
                  {conv.archived ? <ArchiveRestore /> : <Archive />}
                  {conv.archived
                    ? t("sidebar.unarchiveChat")
                    : t("sidebar.archiveChat")}
                </DropdownMenuItem>

                <DropdownMenuSeparator />

                <DropdownMenuItem
                  onSelect={(e) => {
                    if (!confirmDelete) {
                      // First click arms the confirm; keep the menu open
                      e.preventDefault();
                      setConfirmDelete(true);
                    } else {
                      onDelete(conv.id);
                    }
                  }}
                  className={cn(
                    "text-destructive focus:text-destructive focus:bg-destructive/10 [&_svg]:text-destructive",
                    confirmDelete && "bg-destructive/10 font-semibold",
                  )}
                >
                  <Trash2 />
                  {confirmDelete
                    ? t("sidebar.confirmDelete")
                    : t("sidebar.delete")}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </>
        ))}
    </div>
  );
}
