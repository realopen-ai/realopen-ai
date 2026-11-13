import { useEffect, useState } from "react";
import { FileText, X } from "lucide-react";
import { fetchChatCommandSources } from "@/api/client";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import { useT } from "@/store/settingsStore";

export function SelectedCommandSources({
  ids,
  titles,
  conversationId,
  className,
  onRemove,
}: {
  ids: string[];
  titles?: Record<string, string>;
  conversationId: string;
  className?: string;
  onRemove?: (id: string) => void;
}) {
  const t = useT();
  const temporary = useChatStore((state) =>
    state.conversations.some(
      (chat) => chat.id === conversationId && chat.temporary,
    ),
  );
  const [resolved, setResolved] = useState<Record<string, string>>({});
  const needsLookup = ids.some((id) => !titles?.[id]);

  // Earlier messages only stored IDs. Resolve names from the chat inventory
  // when opening those messages.
  useEffect(() => {
    if (!needsLookup) return;
    const controller = new AbortController();
    void fetchChatCommandSources(conversationId, temporary, controller.signal)
      .then((items) => {
        if (!controller.signal.aborted) {
          setResolved(
            Object.fromEntries(items.map(({ id, title }) => [id, title])),
          );
        }
      })
      .catch(() => {});
    return () => controller.abort();
  }, [conversationId, needsLookup, temporary]);

  return (
    <ul
      aria-label={t("input.commandSources.selected")}
      className={cn("mt-2 flex min-w-0 flex-wrap gap-1.5", className)}
    >
      {ids.map((id) => {
        const title =
          titles?.[id] ?? resolved[id] ?? t("input.commandSources.unavailable");
        const removeLabel = t("input.commandSources.remove", { title });
        return (
          <li
            key={id}
            className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-md border border-border/70 bg-background/50 px-2 py-1 text-xs text-muted-foreground"
          >
            <FileText aria-hidden="true" className="size-3.5 shrink-0" />
            <span dir="auto" className="truncate" title={title}>
              {title}
            </span>
            {onRemove && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    aria-label={removeLabel}
                    onClick={() => onRemove(id)}
                    className="inline-flex size-5 shrink-0 cursor-pointer items-center justify-center rounded hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <X aria-hidden="true" className="size-3.5" />
                  </button>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-xs wrap-anywhere">
                  {removeLabel}
                </TooltipContent>
              </Tooltip>
            )}
          </li>
        );
      })}
    </ul>
  );
}
