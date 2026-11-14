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
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

export function CitationPreviewDialog({
  source,
  onClose,
}: {
  source: SourceLocation;
  onClose: () => void;
}) {
  const t = useT();
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        className="flex h-[85dvh] max-w-lg flex-col overflow-hidden p-0 [&>button]:hidden"
        aria-describedby={undefined}
      >
        <DialogTitle className="sr-only">{t("learn.viewSource")}</DialogTitle>
        <CitationPreview source={source} onClose={onClose} inDialog />
      </DialogContent>
    </Dialog>
  );
}

export function CitationPreview({
  source,
  onClose,
  className,
  inDialog = false,
}: {
  source: SourceLocation;
  onClose: () => void;
  className?: string;
  inDialog?: boolean;
}) {
  const t = useT();
  const [page, setPage] = useState<number | null>(source.page ?? null);
  const [count, setCount] = useState(0);
  const [mtime, setMtime] = useState<number | null>(null);
  const [excerpt, setExcerpt] = useState(source.excerpt ?? "");
  const [title, setTitle] = useState(source.title ?? "");
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (inDialog) return;
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) onClose();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [inDialog, onClose]);
  useEffect(() => {
    if (!source.documentId || source.artifact) return;
    const controller = new AbortController();
    Promise.all([
      getDocumentPages(source.documentId, controller.signal),
      getDocument(source.documentId, controller.signal),
    ])
      .then(([manifest, doc]) => {
        if (controller.signal.aborted) return;
        const chunk = doc?.chunks?.find((c) => c.id === source.chunk);
        if (!source.title && doc) setTitle(doc.filename);
        const requestedPage = source.page ?? chunk?.page_number ?? null;
        setPage(requestedPage);
        if (chunk) setExcerpt(source.excerpt || chunk.text);
        if (manifest) {
          setCount(manifest.count);
          setMtime(manifest.source_mtime);
          if (requestedPage === null && !source.chunk) setPage(1);
          setFailed(
            requestedPage !== null &&
              (requestedPage < 1 || requestedPage > manifest.count),
          );
        } else setFailed(true);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [source]);
  const href = sourceLocationUrl({ ...source, page });
  return (
    <aside
      aria-label={t("learn.viewSource")}
      className={cn("flex h-full min-h-0 flex-col bg-card", className)}
    >
      {source.artifact ? (
        <NotebookSourcePreview
          id={source.artifact.artifact_id}
          title={source.title ?? t("learn.viewSource")}
          location={source.artifact}
          embedded
          onClose={onClose}
        />
      ) : (
        <>
          <header className="flex gap-3 items-center p-4 border-b border-border">
            <h2 dir="auto" className="min-w-0 flex-1 truncate font-medium">
              {title || t("learn.viewSource")}
            </h2>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={onClose}
              aria-label={t("common.close")}
            >
              <X className="size-4" />
            </Button>
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
            ) : failed || !page || !source.documentId ? (
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
                    if (Number.isSafeInteger(n) && n >= 1 && n <= count)
                      setPage(n);
                  }}
                  className="w-20 border border-border rounded px-2 py-1"
                />
                <span dir="ltr" className="text-sm ms-2">
                  {page} / {count}
                </span>
                <img
                  src={documentPageUrl(source.documentId, page, "full", mtime)}
                  alt={t("workspace.documents.pageAlt", {
                    page,
                    filename: title,
                  })}
                  className="w-full"
                  onError={() => setFailed(true)}
                />
              </>
            )}
          </div>
          {href && (
            <Link
              to={href}
              onClick={onClose}
              className="p-4 border-t border-border text-sm text-primary"
            >
              {t("sources.openFullFile")}
            </Link>
          )}
        </>
      )}
    </aside>
  );
}
