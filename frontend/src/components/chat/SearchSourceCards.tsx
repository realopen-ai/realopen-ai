import { useState } from "react";
import { FileText } from "lucide-react";
import { Link } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { documentThumbnailUrl } from "@/api/documentsClient";
import { SourceAction } from "@/components/learn/SourceAction";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import { useSettingsStore, useT } from "@/store/settingsStore";

interface SearchSource {
  id: string;
  name: string;
  kind: "document" | "artifact";
  size?: number;
  pages?: number | null;
  chunks?: number;
  createdAt?: number | null;
}

function parseSources(output: string): {
  sources: SearchSource[];
  hasMore: boolean;
} {
  try {
    const value: { sources?: unknown; hasMore?: unknown } | null =
      JSON.parse(output);
    if (Array.isArray(value?.sources)) {
      const sources = value.sources.filter(
        (item): item is SearchSource =>
          item &&
          typeof item.id === "string" &&
          typeof item.name === "string" &&
          (item.kind === "document" || item.kind === "artifact"),
      );
      return { sources, hasMore: value.hasMore === true };
    }
  } catch {
    // A malformed or older result can still use the chat's text summary.
  }
  return { sources: [], hasMore: false };
}

function SourceThumbnail({ url }: { url?: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <div className="flex aspect-4/3 items-center justify-center overflow-hidden bg-secondary">
      {url && !failed ? (
        <img
          src={url}
          alt=""
          loading="lazy"
          onError={() => setFailed(true)}
          className="h-full w-full object-cover object-top"
        />
      ) : (
        <span className="rounded-lg bg-background/60 p-3 text-muted-foreground">
          <FileText className="size-5" aria-hidden="true" />
        </span>
      )}
    </div>
  );
}

export function SearchSourceCards({
  output,
  title,
  fallback,
  conversationId,
}: {
  output: string;
  title: string;
  fallback: string;
  conversationId: string;
}) {
  const t = useT();
  const language = useSettingsStore((store) => store.language);
  const dateFormat = new Intl.DateTimeFormat(language, { dateStyle: "medium" });
  const temporary = useChatStore((store) =>
    store.conversations.some(
      (chat) => chat.id === conversationId && chat.temporary,
    ),
  );
  const { sources, hasMore } = parseSources(output);
  if (!sources.length) return <ReactMarkdown>{fallback}</ReactMarkdown>;

  return (
    <section>
      <p className="text-sm font-semibold">{title}</p>
      {hasMore && (
        <p className="text-xs text-muted-foreground">
          {t("input.sources.first50")}
        </p>
      )}
      <ul className="my-2 grid max-h-[28rem] grid-cols-[repeat(auto-fill,minmax(min(100%,11rem),1fr))] gap-2 overflow-y-auto">
        {sources.map((source) => {
          const document = source.kind === "document";
          const id = encodeURIComponent(source.id);
          const thumbnail = !document
            ? undefined
            : temporary
              ? `/api/chat/temporary/${encodeURIComponent(conversationId)}/documents/${id}/thumbnail`
              : documentThumbnailUrl(id);
          const extension = source.name.includes(".")
            ? source.name.split(".").pop()?.toUpperCase()
            : undefined;
          const size =
            typeof source.size === "number"
              ? source.size < 1024
                ? `${source.size} B`
                : source.size < 1024 ** 2
                  ? `${(source.size / 1024).toFixed(1)} KB`
                  : `${(source.size / 1024 ** 2).toFixed(1)} MB`
              : undefined;
          const counts = [
            source.pages
              ? t(
                  source.pages === 1
                    ? "input.sources.onePage"
                    : "input.sources.pageCount",
                  { count: source.pages },
                )
              : undefined,
            typeof source.chunks === "number"
              ? t(
                  source.chunks === 1
                    ? "input.sources.oneChunk"
                    : "input.sources.chunkCount",
                  { count: source.chunks },
                )
              : undefined,
          ]
            .filter(Boolean)
            .join(", ");
          const uploaded =
            typeof source.createdAt === "number" &&
            Number.isFinite(source.createdAt)
              ? dateFormat.format(source.createdAt)
              : undefined;
          const details = [extension, size, counts, uploaded].filter(Boolean);
          return (
            <li
              key={source.id}
              className={cn(
                "relative overflow-hidden rounded-xl border border-border/60 bg-card",
                !temporary &&
                  "transition-colors hover:border-primary/40 hover:bg-surface-hover focus-within:border-primary",
              )}
            >
              <SourceThumbnail key={thumbnail ?? source.id} url={thumbnail} />
              <div className="space-y-1 px-3 py-2.5">
                <p className="truncate text-sm font-medium" title={source.name}>
                  {source.name}
                </p>
                {details.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    {details.join(" · ")}
                  </p>
                )}
                {!temporary &&
                  (document ? (
                    <SourceAction
                      documentId={source.id}
                      title={source.name}
                      className="after:absolute after:inset-0"
                    />
                  ) : (
                    <Link
                      to={`/workspace/artifacts/${id}`}
                      className="text-xs text-muted-foreground hover:text-primary after:absolute after:inset-0"
                    >
                      {t("learn.viewSource")}
                    </Link>
                  ))}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
