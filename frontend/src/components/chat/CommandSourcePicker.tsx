import {
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
  type Ref,
} from "react";
import { FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useT } from "@/store/settingsStore";
import { fetchChatCommandSources, type ChatCommandSource } from "@/api/client";
import { chatSourceThumbnailUrl, SourceThumbnail } from "./SearchSourceCards";

export function CommandSourcePicker({
  ref,
  conversationId,
  temporary,
  command,
  selected,
  onChange,
  onReturnToComposer,
}: {
  ref?: Ref<{ openFromComposer: () => void }>;
  conversationId: string;
  temporary: boolean;
  command: string;
  selected: string[];
  onChange: (ids: string[], titles: Record<string, string>) => void;
  onReturnToComposer: () => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [sources, setSources] = useState<ChatCommandSource[]>([]);
  const [status, setStatus] = useState<"loading" | "loaded" | "error">(
    "loading",
  );
  const openedFromComposer = useRef(false);
  const returnToComposer = useRef(false);
  const menuRef = useRef<HTMLDivElement>(null);
  useImperativeHandle(
    ref,
    () => ({
      openFromComposer() {
        openedFromComposer.current = true;
        setStatus("loading");
        setOpen(true);
      },
    }),
    [],
  );

  useLayoutEffect(() => {
    if (!open || status !== "loaded" || !openedFromComposer.current) return;
    const menu = menuRef.current;
    if (menu && document.activeElement === menu) {
      menu
        .querySelector<HTMLElement>(
          '[role="menuitemcheckbox"]:not([data-disabled])',
        )
        ?.focus();
    }
  }, [open, status]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void fetchChatCommandSources(conversationId, temporary, controller.signal)
      .then((items) => {
        if (controller.signal.aborted) return;
        setSources(items);
        setStatus("loaded");
      })
      .catch(() => {
        if (!controller.signal.aborted) setStatus("error");
      });
    return () => controller.abort();
  }, [conversationId, open, temporary]);

  const single = command === "summarize";
  return (
    <DropdownMenu
      open={open}
      onOpenChange={(next) => {
        if (next) setStatus("loading");
        setOpen(next);
      }}
    >
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="outline" size="sm">
          <FileText className="size-4" />
          {t("input.commandSources.label")}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        ref={menuRef}
        side="top"
        align="start"
        loop
        onEscapeKeyDown={() => {
          returnToComposer.current = true;
        }}
        onKeyDown={(event) => {
          if (event.key !== "ArrowRight") return;
          event.preventDefault();
          returnToComposer.current = true;
          setOpen(false);
        }}
        onCloseAutoFocus={(event) => {
          const shouldReturn =
            openedFromComposer.current || returnToComposer.current;
          openedFromComposer.current = false;
          returnToComposer.current = false;
          if (!shouldReturn) return;
          event.preventDefault();
          onReturnToComposer();
        }}
        className="w-72 max-w-[calc(100vw-2rem)] max-h-72 overflow-y-auto"
      >
        {status !== "loaded" || sources.length === 0 ? (
          <DropdownMenuItem disabled>
            {t(
              `input.commandSources.${status === "loaded" ? "empty" : status}`,
            )}
          </DropdownMenuItem>
        ) : (
          sources.map((source) => {
            const checked = selected.includes(source.id);
            return (
              <DropdownMenuCheckboxItem
                key={source.id}
                checked={checked}
                disabled={!single && !checked && selected.length >= 20}
                onSelect={(event) => {
                  if (!single) event.preventDefault();
                }}
                onCheckedChange={(value) => {
                  const ids = value
                    ? single
                      ? [source.id]
                      : [...selected, source.id]
                    : selected.filter((id) => id !== source.id);
                  onChange(
                    ids,
                    Object.fromEntries(
                      sources
                        .filter((item) => ids.includes(item.id))
                        .map((item) => [item.id, item.title]),
                    ),
                  );
                }}
              >
                <span className="mr-2 w-9 shrink-0 overflow-hidden rounded">
                  <SourceThumbnail
                    url={
                      source.document_id
                        ? chatSourceThumbnailUrl(
                            source.document_id,
                            conversationId,
                            temporary,
                          )
                        : undefined
                    }
                  />
                </span>
                <span className="min-w-0 truncate" title={source.title}>
                  {source.title}
                </span>
              </DropdownMenuCheckboxItem>
            );
          })
        )}
        {selected.length > 0 && (
          <DropdownMenuItem onSelect={() => onChange([], {})}>
            {t("input.commandSources.clear")}
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
