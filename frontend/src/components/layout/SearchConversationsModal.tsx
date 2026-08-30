import { useEffect, useState } from "react";
import { X, Search } from "lucide-react";
import { HistoryTab } from "@/components/brain/HistoryTab";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";

// Duration of the exit animation (must match the *-out animation durations in index.css)
const EXIT_ANIMATION_MS = 170;

const isMac =
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/.test(navigator.userAgent);

// ─── Search Conversations Modal ─────────────────────────────────
// Centered modal with a dark overlay that offers the same
// past-conversation keyword search as the Brain page's History tab.

export function SearchConversationsModal({
  open,
  onClose,
  onOpenConversation,
}: {
  open: boolean;
  onClose: () => void;
  onOpenConversation: (conversationId: string) => void;
}) {
  const t = useT();

  // Mount / exit-animation lifecycle:
  //   open=true  → mount immediately, play the "in" animation
  //   open=false → play the "out" animation, unmount after it finishes
  const [mounted, setMounted] = useState(open);
  const [closing, setClosing] = useState(false);

  useEffect(() => {
    if (open) {
      setMounted(true);
      setClosing(false);
      return;
    }
    if (!mounted) return;
    setClosing(true);
    const timer = setTimeout(() => {
      setMounted(false);
      setClosing(false);
    }, EXIT_ANIMATION_MS);
    return () => clearTimeout(timer);
  }, [open, mounted]);

  // Escape closes the modal (works even while the search input is focused)
  useEffect(() => {
    if (!mounted) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [mounted, onClose]);

  if (!mounted) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("sidebar.searchConversations")}
    >
      {/* Backdrop (dark overlay) */}
      <div
        className={cn(
          "absolute inset-0 bg-black/70 backdrop-blur-sm",
          closing ? "animate-overlay-out" : "animate-overlay-in",
        )}
        onClick={onClose}
      />

      {/* Modal panel */}
      <div
        className={cn(
          "relative flex w-full max-w-xl flex-col h-[70vh] max-h-150",
          "bg-card border border-border rounded-2xl shadow-2xl overflow-hidden",
          closing ? "animate-modal-out" : "animate-modal-in",
        )}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-border/50 shrink-0">
          <div className="flex items-center gap-2">
            <Search className="w-4 h-4 text-primary" />
            <h2 className="text-[15px] font-semibold text-foreground">
              {t("sidebar.searchConversations")}
            </h2>
          </div>
          <button
            onClick={onClose}
            aria-label={t("common.close")}
            className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body — same search as the Brain page's History tab */}
        <div className="flex-1 min-h-0">
          <HistoryTab onOpenConversation={onOpenConversation} />
        </div>

        {/* Footer — keyboard hints */}
        <div className="flex items-center gap-4 px-5 py-2.5 border-t border-border/50 shrink-0">
          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground/60">
            <kbd className="px-1.5 py-0.5 rounded-md border border-border bg-secondary text-[10px] font-mono text-muted-foreground">
              esc
            </kbd>
            {t("sidebar.searchHintClose")}
          </span>
          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground/60">
            <kbd className="px-1.5 py-0.5 rounded-md border border-border bg-secondary text-[10px] font-mono text-muted-foreground">
              {isMac ? "⌘" : "Ctrl"} K
            </kbd>
            {t("sidebar.searchHintToggle")}
          </span>
        </div>
      </div>
    </div>
  );
}
