import { useState, useRef, useCallback } from "react";
import { Search, X, Loader2, History, ArrowUpRight } from "lucide-react";
import { useT } from "@/store/settingsStore";
import {
  searchPastConversations,
  type PastConversationResult,
} from "@/api/client";
import { useUIStore } from "@/store/uiStore";
import { EmptyState } from "@/components/ui/primitives";
import { Button } from "@/components/ui/button";

// ─── Relative time ──────────────────────────────────────────────

function relativeTime(timestamp: number): string {
  const diff = Math.floor(Date.now() / 1000 - timestamp);

  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;

  return `${Math.floor(diff / 86400)}d ago`;
}

// ─── Excerpt with highlighted query matches ──────────────────────

function HighlightedExcerpt({ text, query }: { text: string; query: string }) {
  const needle = query.trim();
  if (!needle) return <>{text}</>;

  const parts: { value: string; match: boolean }[] = [];
  const lowerText = text.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  let cursor = 0;

  while (cursor < text.length) {
    const idx = lowerText.indexOf(lowerNeedle, cursor);
    if (idx === -1) {
      parts.push({ value: text.slice(cursor), match: false });
      break;
    }
    if (idx > cursor) {
      parts.push({ value: text.slice(cursor, idx), match: false });
    }
    parts.push({ value: text.slice(idx, idx + needle.length), match: true });
    cursor = idx + needle.length;
  }

  return (
    <>
      {parts.map((part, i) =>
        part.match ? (
          <mark
            key={i}
            className="rounded-sm bg-primary/15 px-0.5 text-foreground"
          >
            {part.value}
          </mark>
        ) : (
          <span key={i}>{part.value}</span>
        ),
      )}
    </>
  );
}

// ─── Result row ──────────────────────────────────────────────────

function ResultRow({
  result,
  query,
  onOpen,
  t,
}: {
  result: PastConversationResult;
  query: string;
  onOpen: (conversationId: string) => void;
  t: (key: string) => string;
}) {
  return (
    <article className="group flex items-start gap-3 rounded-lg py-3.5 pl-2 pr-1 transition-colors hover:bg-surface-hover">
      <div className="min-w-0 flex-1">
        {/* Title + metadata */}
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="truncate text-[13.5px] font-medium text-foreground">
            {result.conversation_title}
          </h3>
          <p className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
            <span>{result.role}</span>
            <span aria-hidden="true" className="text-muted-foreground/70">
              ·
            </span>
            <span>{relativeTime(result.created_at)}</span>
            <span aria-hidden="true" className="text-muted-foreground/70">
              ·
            </span>
            <span className="tabular-nums">
              {Math.round(result.rank * 100)}%
            </span>
          </p>
        </div>
        {/* Clamped excerpt */}
        <p className="mt-1.5 line-clamp-3 wrap-break-word text-[13px] leading-relaxed text-muted-foreground">
          <HighlightedExcerpt text={result.content_snippet} query={query} />
        </p>
        {/* Open conversation */}
        <Button
          variant="ghost"
          size="xs"
          className="mt-2 -ml-1.5"
          onClick={() => onOpen(result.conversation_id)}
        >
          <ArrowUpRight />
          {t("brain.history.open") || "Open conversation"}
        </Button>
      </div>
    </article>
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
      <div className="space-y-3 pb-5">
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
              className="h-9 w-full rounded-lg border border-border/60 bg-transparent pl-9 pr-8 text-[13px] text-foreground placeholder:text-muted-foreground/80 transition-colors focus:border-primary/50 focus:outline-none focus:ring-2 focus:ring-primary/20"
              autoFocus
            />
            {query && (
              <button
                onClick={handleClear}
                aria-label={t("brain.history.search")}
                className="absolute right-2 top-1/2 flex h-5 w-5 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground/80 transition-colors hover:bg-surface-hover hover:text-foreground"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
        </div>

        {/* Info hint — quiet helper text until the first search */}
        {!hasSearched && (
          <div className="flex items-start gap-2 px-3 py-2.5 rounded-xl text-[12px] text-muted-foreground">
            <History className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            <span>
              {t("brain.history.info") ||
                "Search through all your past conversation messages by keyword. The AI can also search these automatically when you ask about something discussed previously."}
            </span>
          </div>
        )}
      </div>

      {/* Results */}
      <div className="divide-y divide-border/50">
        {/* Searching state */}
        {isSearching && (
          <div className="flex items-center justify-center py-8">
            <div className="flex items-center gap-2 text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span className="text-xs">Searching...</span>
            </div>
          </div>
        )}

        {/* Empty search results */}
        {!isSearching && hasSearched && results.length === 0 && (
          <EmptyState
            icon={<Search />}
            title={
              t("brain.history.empty") || "No matching conversations found."
            }
          />
        )}

        {/* Result rows */}
        {!isSearching &&
          results.map((r) => (
            <ResultRow
              key={r.message_id}
              result={r}
              query={query}
              onOpen={handleOpen}
              t={t}
            />
          ))}
      </div>

      {/* Result count */}
      {!isSearching && results.length > 0 && (
        <p className="pt-4 text-center text-xs text-muted-foreground/80">
          {results.length} {results.length === 1 ? "result" : "results"}
        </p>
      )}
    </div>
  );
}
