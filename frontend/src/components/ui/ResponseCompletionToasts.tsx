import { useEffect, useState } from "react";
import { Check, X } from "lucide-react";
import {
  openCompletedConversation,
  type ResponseCompletion,
} from "@/lib/responseNotifications";
import { useT } from "@/store/settingsStore";

type ToastItem = ResponseCompletion & { timeout: number };

export function ResponseCompletionToasts() {
  const [items, setItems] = useState<ToastItem[]>([]);
  const t = useT();

  useEffect(() => {
    const onToast = (event: Event) => {
      const detail = (event as CustomEvent<ResponseCompletion>).detail;
      const timeout = window.setTimeout(
        () => setItems((current) => current.filter((item) => item.responseId !== detail.responseId)),
        6500,
      );
      setItems((current) => [...current, { ...detail, timeout }]);
    };
    window.addEventListener("response-completion-toast", onToast);
    return () => window.removeEventListener("response-completion-toast", onToast);
  }, []);

  const dismiss = (item: ToastItem) => {
    window.clearTimeout(item.timeout);
    setItems((current) => current.filter((candidate) => candidate.responseId !== item.responseId));
  };

  return (
    <div className="pointer-events-none fixed end-4 top-4 z-[100] flex w-[min(360px,calc(100vw-2rem))] flex-col gap-2">
      {items.map((item) => (
        <div
          key={item.responseId}
          role="status"
          className="pointer-events-auto flex cursor-pointer items-start gap-3 rounded-xl border border-border/70 bg-card p-3.5 shadow-[0_16px_48px_var(--color-shadow-strong)] animate-fade-in"
          onClick={() => {
            dismiss(item);
            openCompletedConversation(item.conversationId);
          }}
        >
          <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-success/10 text-success">
            <Check className="h-4 w-4" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-[13.5px] font-medium text-foreground">
              {t("notifications.responseCompleted")}
            </div>
            <div className="mt-0.5 truncate text-xs text-muted-foreground">
              {item.conversationTitle}
            </div>
          </div>
          <button
            type="button"
            aria-label={t("common.close")}
            className="rounded-md p-1 text-muted-foreground hover:bg-surface-hover hover:text-foreground"
            onClick={(event) => {
              event.stopPropagation();
              dismiss(item);
            }}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}
