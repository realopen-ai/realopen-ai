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
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
 *
 * When the sidebar is collapsed the row shrinks to a centered icon with
 * a tooltip (conversation title) and a quiet selected state (surface +
 * 2px primary indicator on the leading edge).
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

  const leadIcon = conv.pinned ? (
    <Pin className="size-4 shrink-0 text-primary fill-primary/20" />
  ) : (
    collapsed && (
      <MessageSquare
        className={cn(
          "size-4 shrink-0",
          active ? "text-foreground/70" : "text-muted-foreground/80",
        )}
      />
    )
  );

  // ── Collapsed rail row: centered icon + tooltip + selected indicator ──
  if (collapsed) {
    return (
      <Tooltip delayDuration={300}>
        <TooltipTrigger asChild>
          <div
            onClick={() => !renaming && onSelect(conv.id)}
            className={cn(
              "relative flex h-9 w-9 cursor-pointer items-center justify-center rounded-lg transition-colors",
              active
                ? "bg-surface-selected text-foreground"
                : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
            )}
          >
            {active && (
              <span className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full bg-primary" />
            )}
            {leadIcon}
          </div>
        </TooltipTrigger>
        <TooltipContent
          side="right"
          sideOffset={8}
          className="max-w-52 truncate"
        >
          {conv.title}
        </TooltipContent>
      </Tooltip>
    );
  }

  // ── Expanded row ────────────────────────────────────────────────────
  return (
    <div
      onClick={() => !renaming && onSelect(conv.id)}
      className={cn(
        "group flex h-9 w-full cursor-pointer items-center gap-2.5 rounded-lg px-2.5 transition-colors",
        active
          ? "bg-surface-selected text-foreground"
          : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
      )}
    >
      {leadIcon}

      {renaming ? (
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
          className="h-7 w-full min-w-0 flex-1 rounded-md border border-border bg-background px-1.5 text-[13px] text-foreground outline-none transition-colors focus:border-primary/60 focus:ring-2 focus:ring-primary/25"
          aria-label={t("sidebar.rename")}
          maxLength={120}
        />
      ) : (
        <>
          <span
            className="min-w-0 flex-1 truncate text-[13.5px]"
            title={conv.title}
          >
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
                className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground opacity-0 transition-opacity hover:bg-secondary hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 group-hover:opacity-100 data-[state=open]:opacity-100"
              >
                <MoreHorizontal className="size-4" />
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

              <DropdownMenuItem onSelect={() => togglePinConversation(conv.id)}>
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
      )}
    </div>
  );
}
