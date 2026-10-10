import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { X, Loader2 } from "lucide-react";
import {
  getDocument,
  getDocumentPages,
  documentPageUrl,
} from "@/api/documentsClient";
import { useT } from "@/store/settingsStore";
import { sourceLocationUrl, type SourceLocation } from "@/lib/sourceNavigation";
import { NotebookSourcePreview } from "./NotebookSourcePreview";

export function NotebookCitationPreview({
  source,
  onClose,
}: {
  source: SourceLocation;
  onClose: () => void;
}) {
  const t = useT();
  const [page, setPage] = useState(source.page ?? 1);
  const [count, setCount] = useState(0);
  const [mtime, setMtime] = useState<number | null>(null);
  const [excerpt, setExcerpt] = useState(source.excerpt ?? "");
  const [title, setTitle] = useState(source.title ?? "");
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [onClose]);
  useEffect(() => {
    if (!source.documentId || source.artifact) return;
    let cancelled = false;
    Promise.all([
      getDocumentPages(source.documentId),
      getDocument(source.documentId),
    ])
      .then(([manifest, doc]) => {
        if (cancelled) return;
        const chunk = doc?.chunks?.find((c) => c.id === source.chunk);
        if (!source.title && doc) setTitle(doc.filename);
        const requestedPage = source.page ?? chunk?.page_number ?? 1;
        if (manifest) {
          setCount(manifest.count);
          setMtime(manifest.source_mtime);
          setPage(Math.max(1, Math.min(requestedPage, manifest.count)));
        } else setFailed(true);
        if (!source.excerpt && chunk) setExcerpt(chunk.text);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [source]);
  if (source.artifact)
    return (
      <NotebookSourcePreview
        id={source.artifact.artifact_id}
        title={source.title ?? t("learn.viewSource")}
        location={source.artifact}
        inline
        onClose={onClose}
      />
    );
  const href = sourceLocationUrl({ ...source, page });
  return (
    <aside
      aria-label={t("learn.viewSource")}
      className="pointer-events-auto absolute inset-y-24 xl:inset-y-14 end-0 z-30 w-full xl:w-[min(50%,40rem)] bg-card border-s border-border shadow-xl flex flex-col"
    >
      <header className="flex gap-3 items-center p-4 border-b border-border">
        <h2 dir="auto" className="min-w-0 flex-1 truncate font-medium">
          {title || t("learn.viewSource")}
        </h2>
        <button onClick={onClose} aria-label={t("common.close")}>
          <X className="size-4" />
        </button>
      </header>
      <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
        {excerpt && (
          <blockquote
            dir="auto"
            className="max-h-48 overflow-y-auto border-s-2 border-primary bg-primary/10 p-3 text-sm whitespace-pre-wrap text-start"
          >
            {excerpt}
          </blockquote>
        )}
        {loading ? (
          <Loader2 className="size-5 animate-spin" />
        ) : failed ? (
          <p role="status">{t("workspace.documents.previewUnavailable")}</p>
        ) : (
          <>
            <input
              aria-label={t("workspace.documents.pages")}
              type="number"
              min={1}
              max={count}
              value={page}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (Number.isSafeInteger(n) && n >= 1 && n <= count) setPage(n);
              }}
              className="w-20 border border-border rounded px-2 py-1"
            />
            <span dir="ltr" className="text-sm ms-2">
              {page} / {count}
            </span>
            <img
              src={documentPageUrl(source.documentId!, page, "full", mtime)}
              alt={t("workspace.documents.pageAlt", { page, filename: title })}
              className="w-full"
              onError={() => setFailed(true)}
            />
          </>
        )}
      </div>
      {href && (
        <Link
          to={href}
          className="p-4 border-t border-border text-sm text-primary"
        >
          {t("notebooks.openSource")}
        </Link>
      )}
    </aside>
  );
}
