import { useState, useEffect, useCallback, useRef } from "react";
import {
  X,
  Check,
  Loader2,
  AlertCircle,
  Download,
  Trash2,
  Pencil,
  ChevronLeft,
  ChevronRight,
  Plus,
  RefreshCw,
  Brain,
  FileText,
  FileSpreadsheet,
  Presentation,
  Table,
  BookOpen,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  downloadDocument,
  deleteDocument,
  updateDocument,
  getDocumentPages,
  documentPageUrl,
  reindexDocumentStream,
  removeDocumentKnowledge,
  type DocumentDTO,
  type DigestProgress,
} from "@/api/documentsClient";

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(ms: number): string {
  if (!ms) return "—";
  return new Date(ms).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function fileExt(name: string): string {
  return name.split(".").pop()?.toLowerCase() || "";
}

function TypeIcon({ name }: { name: string }) {
  const ext = fileExt(name);
  if (ext === "pdf")
    return <FileText className="h-5 w-5 text-red-600 dark:text-red-400" />;
  if (["docx", "doc"].includes(ext))
    return <FileText className="h-5 w-5 text-blue-600 dark:text-blue-400" />;
  if (["xlsx", "xls"].includes(ext))
    return (
      <FileSpreadsheet className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />
    );
  if (["pptx", "ppt"].includes(ext))
    return (
      <Presentation className="h-5 w-5 text-amber-600 dark:text-amber-400" />
    );
  if (["csv", "tsv"].includes(ext))
    return <Table className="h-5 w-5 text-violet-600 dark:text-violet-400" />;
  return <FileText className="h-5 w-5 text-muted-foreground" />;
}

type KnowledgeAction = "reindex" | "remove" | null;

const sectionLabelClass = "mb-2.5 text-xs font-medium text-muted-foreground";

export function DocumentDetailModal({
  doc,
  onClose,
  onChanged,
  onDeleted,
}: {
  doc: DocumentDTO;
  onClose: () => void;
  /** Called with the updated doc (or null to trigger a list refresh). */
  onChanged: (doc: DocumentDTO | null) => void;
  onDeleted: (id: string) => void;
}) {
  // ── Preview state ─────────────────────────────────────────────────
  const [pages, setPages] = useState<{
    count: number;
    source_mtime: number | null;
  } | null>(null);
  const [pagesError, setPagesError] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);

  // ── Knowledge state ───────────────────────────────────────────────
  const [action, setAction] = useState<KnowledgeAction>(null);
  const [progress, setProgress] = useState<DigestProgress | null>(null);

  // ── Collections state ─────────────────────────────────────────────
  const [collections, setCollections] = useState<string[]>(
    doc.collections ?? [],
  );
  const [newCollection, setNewCollection] = useState("");
  const [collectionsBusy, setCollectionsBusy] = useState(false);

  // ── Rename state ──────────────────────────────────────────────────
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(doc.filename);
  const renameInputRef = useRef<HTMLInputElement>(null);

  // ── Deletion state ────────────────────────────────────────────────
  const [deleting, setDeleting] = useState(false);

  // Fetch the pages manifest when the modal opens (or after reindex)
  const loadPreview = useCallback(async () => {
    setPagesError(null);
    const manifest = await getDocumentPages(doc.id);
    if (manifest) {
      setPages({ count: manifest.count, source_mtime: manifest.source_mtime });
      setCurrentPage((p) => Math.min(p, manifest.count));
    } else {
      setPagesError("Preview unavailable for this file.");
      setPages(null);
    }
  }, [doc.id]);

  useEffect(() => {
    loadPreview();
  }, [loadPreview]);

  useEffect(() => {
    if (renaming) renameInputRef.current?.focus();
  }, [renaming]);

  // Keyboard: left/right arrows navigate pages, Escape closes
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (pages && e.key === "ArrowLeft")
        setCurrentPage((p) => Math.max(1, p - 1));
      if (pages && e.key === "ArrowRight")
        setCurrentPage((p) => Math.min(pages.count, p + 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pages, onClose]);

  // ── Knowledge actions ─────────────────────────────────────────────
  const handleReindex = async () => {
    setAction("reindex");
    setProgress(null);
    await reindexDocumentStream(doc.id, {
      onProgress: (p) => setProgress(p),
      onDone: (updated) => {
        setAction(null);
        setProgress(null);
        onChanged(updated);
        // The preview stays valid (file unchanged), but refresh anyway to
        // pick up any count changes from re-rendered page images.
        loadPreview();
      },
      onError: () => {
        setAction(null);
        setProgress(null);
        onChanged(null); // refresh the list to get the failed status
      },
    });
  };

  const handleRemoveKnowledge = async () => {
    if (
      !confirm(
        `Remove "${doc.filename}" from AI knowledge? The file is kept, but its indexed chunks and embeddings will be deleted.`,
      )
    )
      return;
    setAction("remove");
    const updated = await removeDocumentKnowledge(doc.id);
    setAction(null);
    if (updated) onChanged(updated);
  };

  // ── Collections handlers ──────────────────────────────────────────
  const persistCollections = async (next: string[]) => {
    setCollections(next);
    setCollectionsBusy(true);
    const updated = await updateDocument(doc.id, { collections: next });
    setCollectionsBusy(false);
    if (updated) onChanged(updated);
  };

  const addCollection = () => {
    const value = newCollection.trim();
    if (!value) return;
    if (collections.some((c) => c.toLowerCase() === value.toLowerCase())) {
      setNewCollection("");
      return;
    }
    setNewCollection("");
    persistCollections([...collections, value]);
  };

  const removeCollection = (name: string) => {
    persistCollections(collections.filter((c) => c !== name));
  };

  // ── Rename / delete ───────────────────────────────────────────────
  const confirmRename = async () => {
    const newName = renameValue.trim();
    setRenaming(false);
    if (!newName || newName === doc.filename) return;
    const updated = await updateDocument(doc.id, { filename: newName });
    if (updated) onChanged(updated);
  };

  const handleDelete = async () => {
    if (
      !confirm(
        `Delete "${doc.filename}"? This removes the file and all its chunks.`,
      )
    )
      return;
    setDeleting(true);
    const ok = await deleteDocument(doc.id);
    setDeleting(false);
    if (ok) onDeleted(doc.id);
  };

  // ── Derived knowledge UI state ────────────────────────────────────
  const isReady = doc.digestion_status === "ready";
  const isDigesting =
    doc.digestion_status === "digesting" || action === "reindex";
  const isFailed = doc.digestion_status === "failed";

  const totalPages = pages?.count ?? doc.total_pages ?? 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-border/60 bg-card shadow-[0_24px_70px_-12px_var(--color-shadow-strong)]"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={doc.filename}
      >
        {/* Header */}
        <div className="flex h-12 shrink-0 items-center gap-3 border-b border-border/60 px-5">
          <TypeIcon name={doc.filename} />
          {renaming ? (
            <input
              ref={renameInputRef}
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") confirmRename();
                if (e.key === "Escape") setRenaming(false);
              }}
              onBlur={confirmRename}
              className="h-8 min-w-0 flex-1 rounded-lg border border-primary/50 bg-transparent px-2.5 text-[15px] font-semibold text-foreground outline-none"
            />
          ) : (
            <div className="flex min-w-0 flex-1 items-center gap-1.5">
              <h2
                className="truncate text-[15px] font-semibold text-foreground"
                title={doc.original_filename}
              >
                {doc.filename}
              </h2>
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={() => {
                  setRenameValue(doc.filename);
                  setRenaming(true);
                }}
                aria-label="Rename document"
                title="Rename"
                className="shrink-0"
              >
                <Pencil />
              </Button>
            </div>
          )}
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            aria-label="Close"
          >
            <X />
          </Button>
        </div>

        {/* Body */}
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="grid md:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            {/* ── Preview column ─────────────────────────────────── */}
            <div className="border-b border-border/60 p-5 md:border-b-0 md:border-r">
              <p className={sectionLabelClass}>Preview</p>

              {pages ? (
                <div>
                  <div className="relative flex items-center justify-center overflow-hidden rounded-lg border border-border/60 bg-secondary/40">
                    <img
                      src={documentPageUrl(
                        doc.id,
                        currentPage,
                        "full",
                        pages.source_mtime,
                      )}
                      alt={`Page ${currentPage} of ${doc.filename}`}
                      className="max-h-80 w-auto object-contain"
                    />
                    {pages.count > 1 && (
                      <>
                        <button
                          onClick={() =>
                            setCurrentPage((p) => Math.max(1, p - 1))
                          }
                          disabled={currentPage <= 1}
                          className="absolute left-2 flex h-8 w-8 items-center justify-center rounded-full bg-background/80 text-foreground backdrop-blur-sm transition-colors hover:bg-background disabled:opacity-30"
                          title="Previous page"
                        >
                          <ChevronLeft className="h-4 w-4" />
                        </button>
                        <button
                          onClick={() =>
                            setCurrentPage((p) => Math.min(pages.count, p + 1))
                          }
                          disabled={currentPage >= pages.count}
                          className="absolute right-2 flex h-8 w-8 items-center justify-center rounded-full bg-background/80 text-foreground backdrop-blur-sm transition-colors hover:bg-background disabled:opacity-30"
                          title="Next page"
                        >
                          <ChevronRight className="h-4 w-4" />
                        </button>
                      </>
                    )}
                  </div>
                  {pages.count > 1 && (
                    <div className="mt-2.5 flex flex-wrap items-center justify-center gap-1.5">
                      {Array.from({ length: pages.count }, (_, i) => i + 1).map(
                        (n) => (
                          <button
                            key={n}
                            onClick={() => setCurrentPage(n)}
                            className={cn(
                              "h-6 w-6 rounded-md text-[10.5px] font-medium transition-colors",
                              n === currentPage
                                ? "bg-primary/15 text-primary"
                                : "text-muted-foreground hover:bg-secondary hover:text-foreground",
                            )}
                          >
                            {n}
                          </button>
                        ),
                      )}
                    </div>
                  )}
                  <p className="mt-2 text-center text-[11px] text-muted-foreground">
                    {currentPage} / {pages.count} page
                    {pages.count !== 1 ? "s" : ""}
                  </p>
                </div>
              ) : pagesError ? (
                <div className="flex h-48 flex-col items-center justify-center rounded-lg border border-dashed border-border/60 bg-secondary/30 px-4 text-center">
                  <BookOpen className="mb-2 h-7 w-7 text-muted-foreground/70" />
                  <p className="text-[12.5px] text-muted-foreground">
                    {pagesError}
                  </p>
                  <p className="mt-0.5 text-[11px] text-muted-foreground/70">
                    Use the download button to open the file directly.
                  </p>
                </div>
              ) : (
                <div className="flex h-48 flex-col items-center justify-center rounded-lg border border-dashed border-border/60 bg-secondary/30">
                  <Loader2 className="mb-2 h-5 w-5 animate-spin text-primary/60" />
                  <p className="text-[12.5px] text-muted-foreground">
                    Rendering preview…
                  </p>
                </div>
              )}
            </div>

            {/* ── Info column ────────────────────────────────────── */}
            <div className="space-y-6 p-5">
              {/* Information */}
              <section>
                <p className={sectionLabelClass}>Information</p>
                <dl className="space-y-2 text-[13px]">
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">Type</dt>
                    <dd className="font-medium uppercase text-foreground">
                      {fileExt(doc.filename) || "—"}
                    </dd>
                  </div>
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">Size</dt>
                    <dd className="font-medium text-foreground">
                      {formatSize(doc.file_size_bytes)}
                    </dd>
                  </div>
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">Added</dt>
                    <dd className="font-medium text-foreground">
                      {formatDate(doc.created_at)}
                    </dd>
                  </div>
                  {doc.updated_at && doc.updated_at !== doc.created_at && (
                    <div className="flex justify-between gap-3">
                      <dt className="text-muted-foreground">Updated</dt>
                      <dd className="font-medium text-foreground">
                        {formatDate(doc.updated_at)}
                      </dd>
                    </div>
                  )}
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">Scope</dt>
                    <dd className="font-medium capitalize text-foreground">
                      {doc.scope}
                    </dd>
                  </div>
                  {totalPages > 0 && (
                    <div className="flex justify-between gap-3">
                      <dt className="text-muted-foreground">Pages</dt>
                      <dd className="font-medium text-foreground">
                        {totalPages}
                      </dd>
                    </div>
                  )}
                </dl>
              </section>

              {/* AI Knowledge */}
              <section>
                <p className={sectionLabelClass}>AI Knowledge</p>

                {isDigesting ? (
                  <div className="rounded-lg border border-primary/25 bg-primary/5 px-3.5 py-3">
                    <div className="flex items-center gap-2 text-[13px] font-medium text-foreground">
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                      {progress
                        ? progress.stage === "done"
                          ? "Done"
                          : progress.stage.replace(/_/g, " ")
                        : "Digesting…"}
                      {progress && (
                        <span className="ml-auto text-xs text-muted-foreground">
                          {progress.percent}%
                        </span>
                      )}
                    </div>
                    {progress && (
                      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-secondary">
                        <div
                          className="h-full rounded-full bg-primary transition-all"
                          style={{ width: `${progress.percent}%` }}
                        />
                      </div>
                    )}
                    {progress?.details && (
                      <p className="mt-1.5 text-[11px] text-muted-foreground">
                        {progress.details}
                      </p>
                    )}
                  </div>
                ) : isReady ? (
                  <div className="space-y-2.5 rounded-lg border border-success/25 bg-success/5 px-3.5 py-3">
                    <div className="flex items-center gap-1.5 text-[13px] font-medium text-success">
                      <Check className="h-4 w-4" /> Indexed for RAG
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {doc.total_chunks} chunk
                      {doc.total_chunks !== 1 ? "s" : ""}
                      {doc.total_images > 0
                        ? ` · ${doc.total_images} image${
                            doc.total_images !== 1 ? "s" : ""
                          }`
                        : ""}{" "}
                      · searchable by{" "}
                      {doc.scope === "public"
                        ? "all conversations"
                        : "its conversation"}
                    </p>
                    <div className="flex flex-wrap gap-2 pt-0.5">
                      <Button
                        variant="outline"
                        size="xs"
                        onClick={handleReindex}
                        disabled={action !== null}
                      >
                        <RefreshCw />
                        Re-index
                      </Button>
                      <Button
                        variant="ghost"
                        size="xs"
                        onClick={handleRemoveKnowledge}
                        disabled={action !== null}
                        className="text-danger hover:bg-danger/10 hover:text-danger"
                      >
                        <X />
                        Remove from AI knowledge
                      </Button>
                    </div>
                  </div>
                ) : isFailed ? (
                  <div className="space-y-2.5 rounded-lg border border-danger/25 bg-danger/5 px-3.5 py-3">
                    <div className="flex items-center gap-1.5 text-[13px] font-medium text-danger">
                      <AlertCircle className="h-4 w-4" /> Indexing failed
                    </div>
                    {doc.digestion_error && (
                      <p
                        className="line-clamp-3 text-xs text-muted-foreground"
                        title={doc.digestion_error}
                      >
                        {doc.digestion_error}
                      </p>
                    )}
                    <Button
                      variant="outline"
                      size="xs"
                      onClick={handleReindex}
                      disabled={action !== null}
                    >
                      <RefreshCw />
                      Retry indexing
                    </Button>
                  </div>
                ) : (
                  <div className="space-y-2.5 rounded-lg border border-border/60 bg-secondary/40 px-3.5 py-3">
                    <div className="flex items-center gap-1.5 text-[13px] font-medium text-foreground">
                      <Brain className="h-4 w-4 text-muted-foreground" /> Not
                      indexed
                    </div>
                    <p className="text-xs text-muted-foreground">
                      The file is stored but not searchable by the AI.
                    </p>
                    <Button
                      size="xs"
                      onClick={handleReindex}
                      disabled={action !== null}
                    >
                      <Brain />
                      Index for RAG
                    </Button>
                  </div>
                )}
              </section>

              {/* Collections */}
              <section>
                <p className={sectionLabelClass}>Collections</p>
                <div className="flex flex-wrap items-center gap-1.5">
                  {collections.length === 0 && !collectionsBusy && (
                    <p className="text-xs text-muted-foreground">
                      No collections yet.
                    </p>
                  )}
                  {collections.map((name) => (
                    <span
                      key={name}
                      className="inline-flex items-center gap-1 rounded-full bg-primary/10 py-1 pl-2.5 pr-1 text-[11.5px] font-medium text-primary ring-1 ring-primary/20"
                    >
                      {name}
                      <button
                        onClick={() => removeCollection(name)}
                        disabled={collectionsBusy}
                        className="flex h-4 w-4 items-center justify-center rounded-full transition-colors hover:bg-primary/20"
                        title={`Remove "${name}"`}
                      >
                        <X className="h-2.5 w-2.5" />
                      </button>
                    </span>
                  ))}
                  {collectionsBusy && (
                    <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground/80" />
                  )}
                </div>
                <div className="mt-2 flex gap-1.5">
                  <input
                    value={newCollection}
                    onChange={(e) => setNewCollection(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") addCollection();
                    }}
                    placeholder="Add a collection…"
                    className="h-9 min-w-0 flex-1 rounded-lg border border-border/60 bg-transparent px-2.5 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/80 focus:border-primary/50"
                  />
                  <Button
                    variant="outline"
                    size="icon-sm"
                    onClick={addCollection}
                    disabled={!newCollection.trim() || collectionsBusy}
                    title="Add collection"
                    aria-label="Add collection"
                  >
                    <Plus />
                  </Button>
                </div>
              </section>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-t border-border/60 px-5">
          <Button
            variant="ghost"
            size="sm"
            onClick={handleDelete}
            disabled={deleting || action !== null}
            className="text-danger hover:bg-danger/10 hover:text-danger"
          >
            {deleting ? <Loader2 className="animate-spin" /> : <Trash2 />}
            Delete
          </Button>
          <Button size="sm" onClick={() => downloadDocument(doc)}>
            <Download />
            Download
          </Button>
        </div>
      </div>
    </div>
  );
}
