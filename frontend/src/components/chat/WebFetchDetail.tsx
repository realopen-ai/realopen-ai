import { useState } from "react";
import { ExternalLink, Loader2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { markdownCodeComponents } from "./MarkdownCodeBlock";
import { webFetchPreview } from "@/lib/webFetchPreview";
import { useT } from "@/store/settingsStore";
import type { ToolCallResult } from "@/store/chatStore";

const PREVIEW_STEP = 12_000;

export function WebFetchDetail({ tc }: { tc: ToolCallResult }) {
  const t = useT();
  const page = webFetchPreview(tc.output, tc.query);
  const [visible, setVisible] = useState(PREVIEW_STEP);
  return (
    <div className="min-w-0 space-y-3 text-[12.5px]">
      {page.title && (
        <p dir="auto" className="font-medium text-foreground">
          {page.title}
        </p>
      )}
      {page.url && (
        <a
          href={page.url}
          target="_blank"
          rel="noopener noreferrer"
          dir="ltr"
          className="flex w-fit max-w-full items-start gap-1.5 text-primary hover:underline"
        >
          <ExternalLink className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 break-all">{page.url}</span>
        </a>
      )}
      {(page.author || page.date) && (
        <p dir="auto" className="text-xs text-muted-foreground">
          {page.author}
          {page.author && page.date ? " · " : ""}
          {page.date && `${t("tool.webfetch.pageDate")}: ${page.date}`}
        </p>
      )}
      {page.content ? (
        <>
          <div
            dir="auto"
            className="prose prose-sm dark:prose-invert max-h-96 max-w-none overflow-auto rounded-lg border border-border/40 bg-background/50 p-3 text-start break-words [&_pre]:overflow-x-auto [&_p]:my-2 [&_table]:text-xs"
          >
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                ...markdownCodeComponents,
                a: ({ href, children }) => (
                  <a href={href} target="_blank" rel="noopener noreferrer">
                    {children}
                  </a>
                ),
                img: () => null,
              }}
            >
              {page.content.slice(0, visible)}
            </ReactMarkdown>
          </div>
          {visible < page.content.length && (
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
              <span className="text-muted-foreground">
                {t("tool.webfetch.previewLimit", {
                  shown: visible.toLocaleString(),
                  total: page.content.length.toLocaleString(),
                })}
              </span>
              <button
                type="button"
                onClick={() => setVisible((count) => count + PREVIEW_STEP)}
                className="font-medium text-primary hover:underline"
              >
                {t("tool.webfetch.showMore")}
              </button>
            </div>
          )}
        </>
      ) : (
        <p className="flex items-center gap-2 text-muted-foreground">
          {tc.status === "running" && (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          )}
          {t(
            tc.status === "running"
              ? "tool.webfetch.running"
              : "tool.webfetch.empty",
          )}
        </p>
      )}
    </div>
  );
}
