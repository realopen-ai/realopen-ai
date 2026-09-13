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
  if (ext === "pdf") return <FileText className="w-5 h-5 text-red-400" />;
  if (["docx", "doc"].includes(ext))
    return <FileText className="w-5 h-5 text-blue-400" />;
  if (["xlsx", "xls"].includes(ext))
    return <FileSpreadsheet className="w-5 h-5 text-emerald-400" />;
  if (["pptx", "ppt"].includes(ext))
    return <Presentation className="w-5 h-5 text-amber-400" />;
  if (["csv", "tsv"].includes(ext))
    return <Table className="w-5 h-5 text-violet-400" />;
  return <FileText className="w-5 h-5 text-muted-foreground" />;
}

type KnowledgeAction = "reindex" | "remove" | null;

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
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-3xl max-h-[88vh] rounded-xl border border-border/60 bg-background shadow-xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-border/40 shrink-0">
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
              className="flex-1 min-w-0 text-[15px] font-semibold px-2 py-1 rounded bg-background border border-primary/50 focus:outline-none"
            />
          ) : (
            <div className="flex-1 min-w-0 flex items-center gap-1.5">
              <h2
                className="text-[15px] font-semibold text-foreground truncate"
                title={doc.original_filename}
              >
                {doc.filename}
              </h2>
              <button
                onClick={() => {
                  setRenameValue(doc.filename);
                  setRenaming(true);
                }}
                className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-secondary shrink-0"
                title="Rename"
              >
                <Pencil className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
          <button
            onClick={onClose}
            className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-secondary"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 min-h-0 overflow-y-auto">
          <div className="grid md:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            {/* ── Preview column ─────────────────────────────────── */}
            <div className="p-4 border-b md:border-b-0 md:border-r border-border/40">
              <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground/60 mb-2">
                Preview
              </p>

              {pages ? (
                <div>
                  <div className="relative rounded-lg border border-border/50 bg-secondary/30 overflow-hidden flex items-center justify-center">
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
                          className="absolute left-2 w-8 h-8 rounded-full bg-background/80 backdrop-blur-sm flex items-center justify-center text-foreground hover:bg-background disabled:opacity-30"
                          title="Previous page"
                        >
                          <ChevronLeft className="w-4 h-4" />
                        </button>
                        <button
                          onClick={() =>
                            setCurrentPage((p) => Math.min(pages.count, p + 1))
                          }
                          disabled={currentPage >= pages.count}
                          className="absolute right-2 w-8 h-8 rounded-full bg-background/80 backdrop-blur-sm flex items-center justify-center text-foreground hover:bg-background disabled:opacity-30"
                          title="Next page"
                        >
                          <ChevronRight className="w-4 h-4" />
                        </button>
                      </>
                    )}
                  </div>
                  {pages.count > 1 && (
                    <div className="flex items-center justify-center gap-1.5 mt-2 flex-wrap">
                      {Array.from({ length: pages.count }, (_, i) => i + 1).map(
                        (n) => (
                          <button
                            key={n}
                            onClick={() => setCurrentPage(n)}
                            className={cn(
                              "w-6 h-6 rounded text-[10.5px] font-medium transition-colors",
                              n === currentPage
                                ? "bg-primary/15 text-primary ring-1 ring-primary/30"
                                : "text-muted-foreground hover:bg-secondary",
                            )}
                          >
                            {n}
                          </button>
                        ),
                      )}
                    </div>
                  )}
                  <p className="text-[10.5px] text-muted-foreground/50 text-center mt-2">
                    {currentPage} / {pages.count} page
                    {pages.count !== 1 ? "s" : ""}
                  </p>
                </div>
              ) : pagesError ? (
                <div className="h-48 rounded-lg border border-dashed border-border/50 bg-secondary/20 flex flex-col items-center justify-center text-center px-4">
                  <BookOpen className="w-7 h-7 text-muted-foreground/25 mb-2" />
                  <p className="text-[11.5px] text-muted-foreground/60">
                    {pagesError}
                  </p>
                  <p className="text-[10.5px] text-muted-foreground/40 mt-0.5">
                    Use the download button to open the file directly.
                  </p>
                </div>
              ) : (
                <div className="h-48 rounded-lg border border-dashed border-border/50 bg-secondary/20 flex flex-col items-center justify-center">
                  <Loader2 className="w-5 h-5 animate-spin text-primary/60 mb-2" />
                  <p className="text-[11.5px] text-muted-foreground/60">
                    Rendering preview…
                  </p>
                </div>
              )}
            </div>

            {/* ── Info column ────────────────────────────────────── */}
            <div className="p-4 space-y-5">
              {/* Information */}
              <section>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground/60 mb-2">
                  Information
                </p>
                <dl className="space-y-1.5 text-[12px]">
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground/70">Type</dt>
                    <dd className="font-medium text-foreground uppercase">
                      {fileExt(doc.filename) || "—"}
                    </dd>
                  </div>
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground/70">Size</dt>
                    <dd className="font-medium text-foreground">
                      {formatSize(doc.file_size_bytes)}
                    </dd>
                  </div>
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground/70">Added</dt>
                    <dd className="font-medium text-foreground">
                      {formatDate(doc.created_at)}
                    </dd>
                  </div>
                  {doc.updated_at && doc.updated_at !== doc.created_at && (
                    <div className="flex justify-between gap-3">
                      <dt className="text-muted-foreground/70">Updated</dt>
                      <dd className="font-medium text-foreground">
                        {formatDate(doc.updated_at)}
                      </dd>
                    </div>
                  )}
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground/70">Scope</dt>
                    <dd className="font-medium text-foreground capitalize">
                      {doc.scope}
                    </dd>
                  </div>
                  {totalPages > 0 && (
                    <div className="flex justify-between gap-3">
                      <dt className="text-muted-foreground/70">Pages</dt>
                      <dd className="font-medium text-foreground">
                        {totalPages}
                      </dd>
                    </div>
                  )}
                </dl>
              </section>

              {/* AI Knowledge */}
              <section>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground/60 mb-2">
                  AI Knowledge
                </p>

                {isDigesting ? (
                  <div className="rounded-lg border border-blue-500/30 bg-blue-500/5 px-3 py-2.5">
                    <div className="flex items-center gap-2 text-[12px] font-medium text-blue-400">
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      {progress
                        ? progress.stage === "done"
                          ? "Done"
                          : progress.stage.replace(/_/g, " ")
                        : "Digesting…"}
                      {progress && (
                        <span className="ml-auto text-[11px] text-muted-foreground/70">
                          {progress.percent}%
                        </span>
                      )}
                    </div>
                    {progress && (
                      <div className="h-1.5 rounded-full bg-secondary/70 overflow-hidden mt-2">
                        <div
                          className="h-full bg-primary transition-all"
                          style={{ width: `${progress.percent}%` }}
                        />
                      </div>
                    )}
                    {progress?.details && (
                      <p className="text-[10.5px] text-muted-foreground/70 mt-1.5">
                        {progress.details}
                      </p>
                    )}
                  </div>
                ) : isReady ? (
                  <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2.5 space-y-2">
                    <div className="flex items-center gap-1.5 text-[12px] font-medium text-emerald-400">
                      <Check className="w-4 h-4" /> Indexed for RAG
                    </div>
                    <p className="text-[11px] text-muted-foreground/70">
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
                      <button
                        onClick={handleReindex}
                        disabled={action !== null}
                        className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-[11.5px] font-medium rounded-md border border-border/50 hover:border-primary/40 hover:text-primary transition-colors disabled:opacity-40"
                      >
                        <RefreshCw className="w-3.5 h-3.5" />
                        Re-index
                      </button>
                      <button
                        onClick={handleRemoveKnowledge}
                        disabled={action !== null}
                        className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-[11.5px] font-medium rounded-md border border-border/50 text-muted-foreground hover:text-red-400 hover:border-red-500/40 transition-colors disabled:opacity-40"
                      >
                        <X className="w-3.5 h-3.5" />
                        Remove from AI knowledge
                      </button>
                    </div>
                  </div>
                ) : isFailed ? (
                  <div className="rounded-lg border border-red-500/30 bg-red-500/5 px-3 py-2.5 space-y-2">
                    <div className="flex items-center gap-1.5 text-[12px] font-medium text-red-400">
                      <AlertCircle className="w-4 h-4" /> Indexing failed
                    </div>
                    {doc.digestion_error && (
                      <p
                        className="text-[11px] text-muted-foreground/70 line-clamp-3"
                        title={doc.digestion_error}
                      >
                        {doc.digestion_error}
                      </p>
                    )}
                    <button
                      onClick={handleReindex}
                      disabled={action !== null}
                      className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-[11.5px] font-medium rounded-md border border-border/50 hover:border-primary/40 hover:text-primary transition-colors disabled:opacity-40"
                    >
                      <RefreshCw className="w-3.5 h-3.5" />
                      Retry indexing
                    </button>
                  </div>
                ) : (
                  <div className="rounded-lg border border-border/50 bg-secondary/20 px-3 py-2.5 space-y-2">
                    <div className="flex items-center gap-1.5 text-[12px] font-medium text-muted-foreground">
                      <Brain className="w-4 h-4" /> Not indexed
                    </div>
                    <p className="text-[11px] text-muted-foreground/70">
                      The file is stored but not searchable by the AI.
                    </p>
                    <button
                      onClick={handleReindex}
                      disabled={action !== null}
                      className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-[11.5px] font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-40"
                    >
                      <Brain className="w-3.5 h-3.5" />
                      Index for RAG
                    </button>
                  </div>
                )}
              </section>

              {/* Collections */}
              <section>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground/60 mb-2">
                  Collections
                </p>
                <div className="flex flex-wrap items-center gap-1.5">
                  {collections.length === 0 && !collectionsBusy && (
                    <p className="text-[11px] text-muted-foreground/50">
                      No collections yet.
                    </p>
                  )}
                  {collections.map((name) => (
                    <span
                      key={name}
                      className="inline-flex items-center gap-1 pl-2.5 pr-1 py-1 rounded-full bg-primary/10 text-primary text-[11.5px] font-medium ring-1 ring-primary/20"
                    >
                      {name}
                      <button
                        onClick={() => removeCollection(name)}
                        disabled={collectionsBusy}
                        className="w-4 h-4 rounded-full flex items-center justify-center hover:bg-primary/20 transition-colors"
                        title={`Remove "${name}"`}
                      >
                        <X className="w-2.5 h-2.5" />
                      </button>
                    </span>
                  ))}
                  {collectionsBusy && (
                    <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground/50" />
                  )}
                </div>
                <div className="flex gap-1.5 mt-2">
                  <input
                    value={newCollection}
                    onChange={(e) => setNewCollection(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") addCollection();
                    }}
                    placeholder="Add a collection…"
                    className="flex-1 min-w-0 px-2.5 py-1.5 text-[11.5px] rounded-md bg-secondary/50 border border-border/40 focus:outline-none focus:border-primary/50"
                  />
                  <button
                    onClick={addCollection}
                    disabled={!newCollection.trim() || collectionsBusy}
                    className="w-8 h-8 rounded-md border border-border/40 flex items-center justify-center text-muted-foreground hover:text-primary hover:border-primary/40 transition-colors disabled:opacity-40"
                    title="Add collection"
                  >
                    <Plus className="w-3.5 h-3.5" />
                  </button>
                </div>
              </section>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-2 px-4 py-3 border-t border-border/40 shrink-0">
          <button
            onClick={handleDelete}
            disabled={deleting || action !== null}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium rounded-md text-muted-foreground hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40"
          >
            {deleting ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Trash2 className="w-3.5 h-3.5" />
            )}
            Delete
          </button>
          <button
            onClick={() => downloadDocument(doc)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
          >
            <Download className="w-3.5 h-3.5" />
            Download
          </button>
        </div>
      </div>
    </div>
  );
}
