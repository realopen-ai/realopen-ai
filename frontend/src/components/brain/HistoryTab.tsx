import { useState, useRef, useCallback } from "react";
import {
  Search,
  X,
  Loader2,
  History,
  MessageSquare,
  ExternalLink,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import {
  searchPastConversations,
  type PastConversationResult,
} from "@/api/client";
import { useUIStore } from "@/store/uiStore";

// ─── Relative time ──────────────────────────────────────────────

function relativeTime(timestamp: number): string {
  const now = Date.now();
  const diff = now - timestamp;
  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (days > 0) return `${days}d ago`;
  if (hours > 0) return `${hours}h ago`;
  if (minutes > 0) return `${minutes}m ago`;
  return "just now";
}

// ─── Result Card ─────────────────────────────────────────────────

function ResultCard({
  result,
  onOpen,
  t,
}: {
  result: PastConversationResult;
  onOpen: (conversationId: string) => void;
  t: (key: string) => string;
}) {
  const roleColor =
    result.role === "user"
      ? "text-primary"
      : result.role === "assistant"
        ? "text-emerald-500"
        : "text-muted-foreground";

  return (
    <div className="group rounded-xl border border-border bg-card p-3.5 transition-all hover:bg-accent/30">
      <div className="flex items-start gap-2">
        <MessageSquare className="w-3.5 h-3.5 text-muted-foreground/50 mt-0.5 shrink-0" />
        <div className="flex-1 min-w-0">
          {/* Header: conversation title + role + time + rank */}
          <div className="flex items-center gap-2 mb-1.5 flex-wrap">
            <span className="text-[12px] font-medium text-foreground truncate max-w-40">
              {result.conversation_title}
            </span>
            <span className={cn("text-[11px] font-medium", roleColor)}>
              {result.role}
            </span>
            <span className="text-[11px] text-muted-foreground/50">
              {relativeTime(result.created_at)}
            </span>
            <span className="text-[11px] text-muted-foreground/40">
              rank {result.rank.toFixed(3)}
            </span>
          </div>
          {/* Snippet */}
          <p className="text-[12.5px] text-foreground/80 leading-relaxed line-clamp-3">
            {result.content_snippet}
          </p>
          {/* Open button */}
          <button
            onClick={() => onOpen(result.conversation_id)}
            className="mt-2 flex items-center gap-1 text-[11px] text-primary hover:text-primary/80 transition-colors"
          >
            <ExternalLink className="w-3 h-3" />
            {t("brain.history.open") || "Open conversation"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── History Tab ─────────────────────────────────────────────────

export function HistoryTab({
  onOpenConversation,
}: {
  onOpenConversation?: (conversationId: string) => void;
}) {
  const t = useT();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PastConversationResult[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const setShowBrainPage = useUIStore((s) => s.setShowBrainPage);

  const handleSearch = useCallback(async (value: string) => {
    setQuery(value);
    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current);
    }
    if (!value.trim()) {
      setResults([]);
      setHasSearched(false);
      return;
    }
    searchTimeoutRef.current = setTimeout(async () => {
      setIsSearching(true);
      setHasSearched(true);
      try {
        const r = await searchPastConversations(value.trim(), undefined, 15);
        setResults(r);
      } catch {
        setResults([]);
      } finally {
        setIsSearching(false);
      }
    }, 350);
  }, []);

  const handleClear = () => {
    setQuery("");
    setResults([]);
    setHasSearched(false);
  };

  const handleOpen = (conversationId: string) => {
    if (onOpenConversation) {
      onOpenConversation(conversationId);
    }
    setShowBrainPage(false);
  };

  return (
    <div className="flex flex-col h-full">
      {/* Search bar */}
      <div className="px-4 pt-4 pb-3 space-y-3">
        <div className="flex items-center gap-2">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground/50" />
            <input
              type="text"
              value={query}
              onChange={(e) => handleSearch(e.target.value)}
              placeholder={
                t("brain.history.search") || "Search past conversations..."
              }
              className="w-full pl-9 pr-9 py-2 rounded-xl border border-border bg-card text-[13px] text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary transition-colors"
              autoFocus
            />
            {query && (
              <button
                onClick={handleClear}
                className="absolute right-2 top-1/2 -translate-y-1/2 w-5 h-5 rounded-full flex items-center justify-center text-muted-foreground/50 hover:text-foreground hover:bg-accent transition-colors"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
        </div>

        {/* Info banner */}
        {!hasSearched && (
          <div className="flex items-start gap-2 px-3 py-2.5 rounded-xl bg-secondary/50 text-[12px] text-muted-foreground">
            <History className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            <span>
              {t("brain.history.info") ||
                "Search through all your past conversation messages by keyword. The AI can also search these automatically when you ask about something discussed previously."}
            </span>
          </div>
        )}
      </div>

      {/* Results */}
      <ScrollArea className="flex-1 px-4">
        <div className="space-y-2 pb-4">
          {/* Searching state */}
          {isSearching && (
            <div className="flex items-center justify-center py-8">
              <div className="flex items-center gap-2 text-muted-foreground">
                <Loader2 className="w-4 h-4 animate-spin" />
                <span className="text-[12px]">Searching...</span>
              </div>
            </div>
          )}

          {/* Empty search results */}
          {!isSearching && hasSearched && results.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 text-center">
              <Search className="w-8 h-8 text-muted-foreground/20 mb-3" />
              <p className="text-[13px] text-muted-foreground/60">
                {t("brain.history.empty") || "No matching conversations found."}
              </p>
            </div>
          )}

          {/* Result cards */}
          {!isSearching &&
            results.map((r) => (
              <ResultCard
                key={r.message_id}
                result={r}
                onOpen={handleOpen}
                t={t}
              />
            ))}

          {/* Result count */}
          {!isSearching && results.length > 0 && (
            <p className="text-center text-[11px] text-muted-foreground/50 pt-2">
              {results.length} {results.length === 1 ? "result" : "results"}
            </p>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
