import { useState, useEffect, useCallback } from "react";
import {
  Upload,
  Search,
  Download,
  Trash2,
  Globe,
  Lock,
  FileText,
  FileSpreadsheet,
  Presentation,
  Table,
  X,
  Check,
  Loader2,
  AlertCircle,
  FolderOpen,
  Files,
  ChevronDown,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { EmptyState, FilterChip } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import {
  listDocuments,
  uploadDocumentStream,
  downloadDocument,
  deleteDocument,
  updateDocument,
  documentThumbnailUrl,
  type DocumentDTO,
  type DocumentScope,
  type DigestProgress,
} from "@/api/documentsClient";
import { fetchConversations, type ConversationDTO } from "@/api/client";
import { DocumentDetailModal } from "@/components/workspace/DocumentDetailModal";
import { t, useT } from "@/store/settingsStore";

// ─── Helpers ─────────────────────────────────────────────────────────

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

const ACCEPTED_EXTS =
  ".txt,.md,.markdown,.csv,.tsv,.pdf,.docx,.doc,.xlsx,.xls,.pptx,.ppt";

function fileExt(name: string): string {
  return name.split(".").pop()?.toLowerCase() || "";
}

// ─── Type groups (filter chips) ──────────────────────────────────────

const TYPE_GROUPS: { key: string; label: string; exts: string[] }[] = [
  { key: "all", label: "All", exts: [] },
  { key: "pdf", label: "PDF", exts: ["pdf"] },
  { key: "word", label: "Word", exts: ["docx", "doc"] },
  { key: "excel", label: "Excel", exts: ["xlsx", "xls"] },
  { key: "slides", label: "Slides", exts: ["pptx", "ppt"] },
  { key: "text", label: "Text", exts: ["txt", "md", "markdown"] },
  { key: "data", label: "Data", exts: ["csv", "tsv"] },
];

function typeOf(doc: DocumentDTO): {
  key: string;
  icon: typeof FileText;
  tint: string;
} {
  const ext = fileExt(doc.filename);
  for (const g of TYPE_GROUPS) {
    if (g.exts.includes(ext)) {
      switch (g.key) {
        case "pdf":
          return {
            key: "pdf",
            icon: FileText,
            tint: "bg-red-500/10 text-red-600 dark:text-red-400",
          };
        case "word":
          return {
            key: "word",
            icon: FileText,
            tint: "bg-blue-500/10 text-blue-600 dark:text-blue-400",
          };
        case "excel":
          return {
            key: "excel",
            icon: FileSpreadsheet,
            tint: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
          };
        case "slides":
          return {
            key: "slides",
            icon: Presentation,
            tint: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
          };
        case "text":
          return {
            key: "text",
            icon: FileText,
            tint: "bg-slate-500/10 text-slate-600 dark:text-slate-400",
          };
        case "data":
          return {
            key: "data",
            icon: Table,
            tint: "bg-violet-500/10 text-violet-600 dark:text-violet-400",
          };
      }
    }
  }
  return {
    key: "other",
    icon: FileText,
    tint: "bg-secondary text-muted-foreground",
  };
}

// ─── Sort options ────────────────────────────────────────────────────

type SortKey =
  | "newest"
  | "oldest"
  | "updated"
  | "updated_asc"
  | "name_asc"
  | "name_desc"
  | "size_desc"
  | "size_asc";

const SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: "newest", label: "Newest upload" },
  { key: "oldest", label: "Oldest upload" },
  { key: "updated", label: "Recently updated" },
  { key: "updated_asc", label: "Least recently updated" },
  { key: "name_asc", label: "Name A→Z" },
  { key: "name_desc", label: "Name Z→A" },
  { key: "size_desc", label: "Largest size" },
  { key: "size_asc", label: "Smallest size" },
];

function sortDocs(docs: DocumentDTO[], key: SortKey): DocumentDTO[] {
  const list = [...docs];
  switch (key) {
    case "newest":
      return list.sort((a, b) => b.created_at - a.created_at);
    case "oldest":
      return list.sort((a, b) => a.created_at - b.created_at);
    case "updated":
      return list.sort((a, b) => b.updated_at - a.updated_at);
    case "updated_asc":
      return list.sort((a, b) => a.updated_at - b.updated_at);
    case "name_asc":
      return list.sort((a, b) => a.filename.localeCompare(b.filename));
    case "name_desc":
      return list.sort((a, b) => b.filename.localeCompare(a.filename));
    case "size_desc":
      return list.sort((a, b) => b.file_size_bytes - a.file_size_bytes);
    case "size_asc":
      return list.sort((a, b) => a.file_size_bytes - b.file_size_bytes);
  }
}

// ─── RAG status line for cards ───────────────────────────────────────

function RagStatus({ doc }: { doc: DocumentDTO }) {
  if (doc.digestion_status === "ready") {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-success">
        <Check className="h-3 w-3" /> RAG: Indexed
      </span>
    );
  }
  if (doc.digestion_status === "digesting") {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" /> RAG: Digesting…
      </span>
    );
  }
  if (doc.digestion_status === "failed") {
    return (
      <span
        className="inline-flex items-center gap-1 text-xs text-danger"
        title={doc.digestion_error ?? ""}
      >
        <AlertCircle className="h-3 w-3" /> RAG: Failed
      </span>
    );
  }
  if (doc.digestion_status === "not_indexed") {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <X className="h-3 w-3" /> RAG: Not indexed
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      <Loader2 className="h-3 w-3 animate-spin" /> RAG: Pending
    </span>
  );
}

// ─── Document card ────────────────────────────────────────────────────

function DocumentCard({
  doc,
  busy,
  onOpen,
  onDownload,
  onDelete,
  onToggleScope,
}: {
  doc: DocumentDTO;
  busy: boolean;
  onOpen: () => void;
  onDownload: () => void;
  onDelete: () => void;
  onToggleScope: () => void;
}) {
  const [thumbError, setThumbError] = useState(false);
  const typeInfo = typeOf(doc);
  const Icon = typeInfo.icon;

  return (
    <div
      className={cn(
        "group relative cursor-pointer overflow-hidden rounded-xl border border-border/60 bg-card transition-colors hover:bg-surface-hover",
        busy && "pointer-events-none opacity-60",
      )}
      onClick={onOpen}
    >
      {/* Thumbnail area */}
      <div className="relative flex aspect-4/3 items-center justify-center overflow-hidden bg-secondary">
        {thumbError ? (
          <div
            className={cn(
              "flex h-12 w-12 items-center justify-center rounded-xl",
              typeInfo.tint,
            )}
          >
            <Icon className="h-6 w-6" />
          </div>
        ) : (
          <img
            src={documentThumbnailUrl(doc.id)}
            alt={`${doc.filename} preview`}
            loading="lazy"
            onError={() => setThumbError(true)}
            className="h-full w-full object-cover object-top"
          />
        )}

        {/* Scope badge */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onToggleScope();
          }}
          title={
            doc.scope === "public"
              ? t("workspace.documents.publicHint")
              : t("workspace.documents.privateHint")
          }
          className="absolute right-2 top-2 inline-flex items-center gap-1 rounded-md bg-background/80 px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground backdrop-blur-sm transition-colors hover:text-foreground"
        >
          {doc.scope === "public" ? (
            <>
              <Globe className="h-3 w-3" /> {t("workspace.documents.public")}
            </>
          ) : (
            <>
              <Lock className="h-3 w-3" /> {t("workspace.documents.private")}
            </>
          )}
        </button>

        {/* Quick actions (hover on desktop) */}
        <div className="absolute left-2 top-2 flex gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDownload();
            }}
            title={t("tool.detail.download")}
            aria-label={t("tool.detail.downloadFile", { filename: doc.filename })}
            className="flex h-7 w-7 items-center justify-center rounded-lg bg-background/80 text-muted-foreground backdrop-blur-sm transition-colors hover:text-foreground"
          >
            <Download className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            title={t("workspace.common.delete")}
            aria-label={t("workspace.documents.deleteFile", { filename: doc.filename })}
            className="flex h-7 w-7 items-center justify-center rounded-lg bg-background/80 text-muted-foreground backdrop-blur-sm transition-colors hover:text-danger"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Info */}
      <div className="p-3.5">
        <p
          className="truncate text-[13.5px] font-medium text-foreground"
          title={doc.original_filename}
        >
          {doc.filename}
        </p>
        <div className="mt-1.5">
          <RagStatus doc={doc} />
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          {formatSize(doc.file_size_bytes)} · {formatDate(doc.created_at)}
        </p>
      </div>
    </div>
  );
}

// ─── Upload Dialog (kept from the Brain tab, pptx/ppt now accepted) ──

interface UploadState {
  open: boolean;
  file: File | null;
  scope: DocumentScope;
  conversationId: string;
  progress: DigestProgress | null;
  isUploading: boolean;
  error: string | null;
  doneDoc: DocumentDTO | null;
}

const initialUpload: UploadState = {
  open: false,
  file: null,
  scope: "public",
  conversationId: "",
  progress: null,
  isUploading: false,
  error: null,
  doneDoc: null,
};

const dialogPanelClass =
  "w-full max-w-md overflow-hidden rounded-xl border border-border/60 bg-card shadow-[0_24px_70px_-12px_var(--color-shadow-strong)]";

const overlayClass =
  "fixed inset-0 z-50 flex items-center justify-center bg-black/65 backdrop-blur-sm p-4";

function DialogChrome({
  title,
  onClose,
  closeDisabled,
  children,
}: {
  title: string;
  onClose: () => void;
  closeDisabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={overlayClass} onClick={onClose}>
      <div
        className={dialogPanelClass}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-border/60 px-4">
          <h2 className="text-[15px] font-semibold text-foreground">{title}</h2>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            disabled={closeDisabled}
            aria-label={t("common.close")}
          >
            <X />
          </Button>
        </div>
        {children}
      </div>
    </div>
  );
}

function UploadDialog({
  state,
  conversations,
  onFileSelect,
  onScopeChange,
  onConversationChange,
  onStart,
  onClose,
}: {
  state: UploadState;
  conversations: ConversationDTO[];
  onFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onScopeChange: (scope: DocumentScope) => void;
  onConversationChange: (cid: string) => void;
  onStart: () => void;
  onClose: () => void;
}) {
  const { progress, isUploading, error, doneDoc, file, scope, conversationId } =
    state;

  return (
    <DialogChrome
      title={t("workspace.documents.uploadDocument")}
      onClose={onClose}
      closeDisabled={isUploading}
    >
      {/* Body */}
      <div className="max-h-[70vh] space-y-4 overflow-y-auto px-4 py-4">
        {/* File picker */}
        <div>
          <label className="mb-1.5 block text-xs font-medium text-foreground">
            {t("workspace.documents.file")}
          </label>
          <input
            type="file"
            accept={ACCEPTED_EXTS}
            onChange={onFileSelect}
            disabled={isUploading}
            className="w-full text-[12px] file:mr-3 file:cursor-pointer file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-1.5 file:text-[12px] file:font-medium file:text-primary-foreground file:transition-colors hover:file:bg-primary/90"
          />
          <p className="mt-1 text-[11px] text-muted-foreground">
            {t("workspace.documents.accepted")}
          </p>
        </div>

        {/* Scope radio */}
        <div>
          <label className="mb-1.5 block text-xs font-medium text-foreground">
            {t("workspace.documents.scope")}
          </label>
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={() => onScopeChange("public")}
              disabled={isUploading}
              className={cn(
                "flex flex-col items-start gap-0.5 rounded-lg border px-3 py-2.5 text-left transition-colors",
                scope === "public"
                  ? "border-primary/40 bg-primary/5"
                  : "border-border/60 hover:bg-surface-hover",
              )}
            >
              <div className="flex items-center gap-1.5">
                <Globe className="h-3.5 w-3.5 text-muted-foreground" />
                <span className="text-[12.5px] font-medium text-foreground">
                  {t("workspace.documents.public")}
                </span>
              </div>
              <span className="text-[11px] text-muted-foreground">
                {t("workspace.documents.publicDescription")}
              </span>
            </button>
            <button
              onClick={() => onScopeChange("private")}
              disabled={isUploading}
              className={cn(
                "flex flex-col items-start gap-0.5 rounded-lg border px-3 py-2.5 text-left transition-colors",
                scope === "private"
                  ? "border-primary/40 bg-primary/5"
                  : "border-border/60 hover:bg-surface-hover",
              )}
            >
              <div className="flex items-center gap-1.5">
                <Lock className="h-3.5 w-3.5 text-muted-foreground" />
                <span className="text-[12.5px] font-medium text-foreground">
                  {t("workspace.documents.private")}
                </span>
              </div>
              <span className="text-[11px] text-muted-foreground">
                {t("workspace.documents.privateDescription")}
              </span>
            </button>
          </div>
        </div>

        {/* Conversation picker (only for private) */}
        {scope === "private" && (
          <div>
            <label className="mb-1.5 block text-xs font-medium text-foreground">
              {t("workspace.documents.attachConversation")}
            </label>
            {conversations.length === 0 ? (
              <div className="flex items-center gap-1.5 text-[12px] text-warning">
                <AlertCircle className="h-3.5 w-3.5" />
                {t("workspace.documents.noConversations")}
              </div>
            ) : (
              <select
                value={conversationId}
                onChange={(e) => onConversationChange(e.target.value)}
                disabled={isUploading}
                className="h-9 w-full cursor-pointer rounded-lg border border-border/60 bg-transparent px-2.5 text-[13px] text-foreground outline-none transition-colors focus:border-primary/50"
              >
                <option value="">{t("workspace.documents.pickConversationOption")}</option>
                {conversations.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.title || t("workspace.documents.untitled")} ·{" "}
                    {new Date(c.createdAt).toLocaleDateString()}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        {/* Progress bar */}
        {progress && (
          <div className="rounded-lg bg-secondary px-3.5 py-3">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-xs font-medium text-foreground">
                {progress.stage === "done"
                  ? t("workspace.documents.done")
                  : progress.stage === "error"
                    ? t("workspace.documents.error")
                    : progress.stage.replace(/_/g, " ")}
              </span>
              <span className="text-xs text-muted-foreground">
                {progress.percent}%
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-border/60">
              <div
                className={cn(
                  "h-full rounded-full transition-all",
                  progress.stage === "error"
                    ? "bg-danger"
                    : progress.stage === "done"
                      ? "bg-success"
                      : "bg-primary",
                )}
                style={{ width: `${progress.percent}%` }}
              />
            </div>
            {progress.details && (
              <p className="mt-1.5 text-[11px] text-muted-foreground">
                {progress.details}
              </p>
            )}
          </div>
        )}

        {/* Done badge */}
        {doneDoc && (
          <div className="flex items-start gap-2 rounded-lg border border-success/30 bg-success/5 px-3.5 py-2.5">
            <Check className="mt-0.5 h-4 w-4 shrink-0 text-success" />
            <div className="text-[12.5px]">
              <p className="font-medium text-success">{t("workspace.documents.ready")}</p>
              <p className="text-muted-foreground">
                {doneDoc.filename}: {doneDoc.total_chunks} chunks
                {doneDoc.total_images > 0
                  ? `, ${doneDoc.total_images} images`
                  : ""}
                .
              </p>
            </div>
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/5 px-3.5 py-2.5">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
            <p className="text-[12.5px] text-danger">{error}</p>
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="flex h-12 shrink-0 items-center justify-end gap-2 border-t border-border/60 px-4">
        <Button
          variant="ghost"
          size="sm"
          onClick={onClose}
          disabled={isUploading}
        >
          {doneDoc ? t("common.close") : t("workspace.cancel")}
        </Button>
        {!doneDoc && (
          <Button
            size="sm"
            onClick={onStart}
            disabled={
              isUploading || !file || (scope === "private" && !conversationId)
            }
          >
            {isUploading ? (
              <>
                <Loader2 className="animate-spin" />
                {t("workspace.documents.uploading")}
              </>
            ) : (
              <>
                <Upload />
                {t("workspace.documents.uploadDigest")}
              </>
            )}
          </Button>
        )}
      </div>
    </DialogChrome>
  );
}

// ─── Scope dialog (replaces window.prompt for public → private) ──────

function ScopeDialog({
  doc,
  conversations,
  onConfirm,
  onCancel,
}: {
  doc: DocumentDTO;
  conversations: ConversationDTO[];
  onConfirm: (conversationId: string) => void;
  onCancel: () => void;
}) {
  const [cid, setCid] = useState("");

  return (
    <DialogChrome title={t("workspace.documents.makePrivate")} onClose={onCancel}>
      <div className="space-y-3 px-4 py-4">
        <p className="text-[13px] text-muted-foreground">
          {t("workspace.documents.makePrivateDescription", { filename: doc.filename })}
        </p>
        {conversations.length === 0 ? (
          <div className="flex items-center gap-1.5 text-[12px] text-warning">
            <AlertCircle className="h-3.5 w-3.5" />
            {t("workspace.documents.noConversations")}
          </div>
        ) : (
          <select
            value={cid}
            onChange={(e) => setCid(e.target.value)}
            className="h-9 w-full cursor-pointer rounded-lg border border-border/60 bg-transparent px-2.5 text-[13px] text-foreground outline-none transition-colors focus:border-primary/50"
          >
            <option value="">{t("workspace.documents.pickConversationOption")}</option>
            {conversations.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title || t("workspace.documents.untitled")} ·{" "}
                {new Date(c.createdAt).toLocaleDateString()}
              </option>
            ))}
          </select>
        )}
      </div>
      <div className="flex h-12 shrink-0 items-center justify-end gap-2 border-t border-border/60 px-4">
        <Button variant="ghost" size="sm" onClick={onCancel}>
          {t("workspace.cancel")}
        </Button>
        <Button size="sm" onClick={() => cid && onConfirm(cid)} disabled={!cid}>
          {t("workspace.documents.makePrivate")}
        </Button>
      </div>
    </DialogChrome>
  );
}

// ─── DocumentsSection ─────────────────────────────────────────────────

const selectClass =
  "h-9 cursor-pointer appearance-none rounded-lg border border-border/60 bg-transparent pl-2.5 pr-7 text-[13px] text-foreground outline-none transition-colors focus:border-primary/50";

export function DocumentsSection() {
  useT();
  const [docs, setDocs] = useState<DocumentDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [filterScope, setFilterScope] = useState<"all" | DocumentScope>("all");
  const [filterType, setFilterType] = useState("all");
  const [sortKey, setSortKey] = useState<SortKey>("newest");
  const [conversations, setConversations] = useState<ConversationDTO[]>([]);
  const [upload, setUpload] = useState<UploadState>(initialUpload);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [detailDoc, setDetailDoc] = useState<DocumentDTO | null>(null);
  const [scopeDialog, setScopeDialog] = useState<DocumentDTO | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    const list = await listDocuments();
    setDocs(list);
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Load conversations for the upload + scope dialogs
  useEffect(() => {
    if ((upload.open || scopeDialog) && conversations.length === 0) {
      fetchConversations(100, 0)
        .then(setConversations)
        .catch(() => {});
    }
  }, [upload.open, scopeDialog, conversations.length]);

  // ── Filtering: search + scope + type, then sorting ────────────────
  const typeGroupExts =
    TYPE_GROUPS.find((g) => g.key === filterType)?.exts ?? [];

  const filtered = sortDocs(
    docs.filter((d) => {
      if (filterScope !== "all" && d.scope !== filterScope) return false;
      if (
        typeGroupExts.length > 0 &&
        !typeGroupExts.includes(fileExt(d.filename))
      )
        return false;
      if (
        search &&
        !d.filename.toLowerCase().includes(search.toLowerCase()) &&
        !d.original_filename.toLowerCase().includes(search.toLowerCase()) &&
        !(d.collections ?? []).some((c) =>
          c.toLowerCase().includes(search.toLowerCase()),
        )
      ) {
        return false;
      }
      return true;
    }),
    sortKey,
  );

  // ── Upload handlers ──────────────────────────────────────────────
  const openUpload = () => setUpload({ ...initialUpload, open: true });

  const closeUpload = () => {
    if (upload.isUploading) return;
    setUpload(initialUpload);
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) setUpload((u) => ({ ...u, file: f, error: null, doneDoc: null }));
  };

  const startUpload = async () => {
    if (!upload.file) return;
    if (upload.scope === "private" && !upload.conversationId) {
      setUpload((u) => ({
        ...u,
        error: t("workspace.documents.pickConversation"),
      }));
      return;
    }
    setUpload((u) => ({
      ...u,
      isUploading: true,
      error: null,
      progress: null,
      doneDoc: null,
    }));

    await uploadDocumentStream(upload.file, {
      scope: upload.scope,
      conversationId:
        upload.scope === "private" ? upload.conversationId : undefined,
      onProgress: (p) => setUpload((u) => ({ ...u, progress: p })),
      onDone: (doc) => {
        setUpload((u) => ({ ...u, isUploading: false, doneDoc: doc }));
        refresh();
      },
      onError: (err) => {
        setUpload((u) => ({ ...u, isUploading: false, error: err }));
      },
    });
  };

  // ── Delete / scope toggle ─────────────────────────────────────────
  const handleDelete = async (doc: DocumentDTO) => {
    if (
      !confirm(
        t("workspace.documents.deleteConfirm", { filename: doc.filename }),
      )
    )
      return;
    setBusyId(doc.id);
    await deleteDocument(doc.id);
    setBusyId(null);
    if (detailDoc?.id === doc.id) setDetailDoc(null);
    refresh();
  };

  const handleToggleScope = async (doc: DocumentDTO) => {
    if (doc.scope === "public") {
      // Public → private: pick a conversation via the scope dialog
      setScopeDialog(doc);
      return;
    }
    // Private → public: no picker needed
    setBusyId(doc.id);
    await updateDocument(doc.id, { scope: "public" });
    setBusyId(null);
    refresh();
  };

  const confirmScopeDialog = async (conversationId: string) => {
    if (!scopeDialog) return;
    setBusyId(scopeDialog.id);
    await updateDocument(scopeDialog.id, {
      scope: "private",
      conversation_id: conversationId,
    });
    setBusyId(null);
    setScopeDialog(null);
    refresh();
  };

  const hasActiveFilters =
    search !== "" || filterScope !== "all" || filterType !== "all";

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="shrink-0 px-6 pt-5 lg:px-10">
        <div className="mx-auto w-full max-w-300 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            {/* Search */}
            <div className="relative min-w-45 flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground/80" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t("workspace.documents.search")}
                className="h-9 w-full rounded-lg border border-border/60 bg-transparent pl-9 pr-3 text-[13.5px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/80 focus:border-primary/50"
              />
            </div>

            {/* Scope filter */}
            <div className="relative">
              <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground/80" />
              <select
                value={filterScope}
                onChange={(e) =>
                  setFilterScope(e.target.value as "all" | DocumentScope)
                }
                className={selectClass}
                title={t("workspace.documents.filterScope")}
              >
                <option value="all">{t("workspace.documents.allScopes")}</option>
                <option value="public">{t("workspace.documents.public")}</option>
                <option value="private">{t("workspace.documents.private")}</option>
              </select>
            </div>

            {/* Sort */}
            <div className="relative">
              <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground/80" />
              <select
                value={sortKey}
                onChange={(e) => setSortKey(e.target.value as SortKey)}
                className={selectClass}
                title={t("workspace.documents.sortOrder")}
              >
                {SORT_OPTIONS.map((o) => (
                  <option key={o.key} value={o.key}>
                    {t(`workspace.documents.sort.${o.key}`)}
                  </option>
                ))}
              </select>
            </div>

            <div className="flex-1" />

            <div className="text-xs text-muted-foreground">
              {t(filtered.length === 1 ? "workspace.documents.count" : "workspace.documents.countPlural", { count: filtered.length })}
            </div>

            <Button size="sm" onClick={openUpload}>
              <Upload />
              {t("workspace.documents.upload")}
            </Button>
          </div>

          {/* Type filter chips */}
          <div className="flex flex-wrap gap-1">
            {TYPE_GROUPS.map((g) => (
              <FilterChip
                key={g.key}
                active={filterType === g.key}
                onClick={() => setFilterType(g.key)}
              >
                {t(`workspace.documents.type.${g.key}`)}
              </FilterChip>
            ))}
          </div>
        </div>
      </div>

      {/* Document card grid */}
      <ScrollArea className="min-h-0 flex-1">
        <div className="mx-auto w-full max-w-300 px-6 pb-16 pt-4 lg:px-10">
          {loading ? (
            <div className="flex flex-col items-center justify-center gap-2 py-16 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
              <p className="text-[13px]">{t("workspace.documents.loading")}</p>
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={hasActiveFilters ? <FolderOpen /> : <Files />}
              title={
                hasActiveFilters
                  ? t("workspace.documents.emptyFiltered")
                  : t("workspace.documents.empty")
              }
              description={
                hasActiveFilters
                  ? t("workspace.documents.emptyFilteredDescription")
                  : t("workspace.documents.emptyDescription")
              }
              action={
                !hasActiveFilters ? (
                  <Button size="sm" onClick={openUpload}>
                    <Upload />
                    {t("workspace.documents.upload")}
                  </Button>
                ) : undefined
              }
              className="rounded-xl"
            />
          ) : (
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
              {filtered.map((doc) => (
                <DocumentCard
                  key={doc.id}
                  doc={doc}
                  busy={busyId === doc.id}
                  onOpen={() => setDetailDoc(doc)}
                  onDownload={() => downloadDocument(doc)}
                  onDelete={() => handleDelete(doc)}
                  onToggleScope={() => handleToggleScope(doc)}
                />
              ))}
            </div>
          )}
        </div>
      </ScrollArea>

      {/* Detail modal */}
      {detailDoc && (
        <DocumentDetailModal
          doc={detailDoc}
          onClose={() => setDetailDoc(null)}
          onChanged={(updated) => {
            if (updated) {
              setDocs((prev) =>
                prev.map((d) => (d.id === updated.id ? updated : d)),
              );
              setDetailDoc(updated);
            } else {
              setDetailDoc(null);
              refresh();
            }
          }}
          onDeleted={(deletedId) => {
            setDetailDoc(null);
            setDocs((prev) => prev.filter((d) => d.id !== deletedId));
          }}
        />
      )}

      {/* Upload Dialog */}
      {upload.open && (
        <UploadDialog
          state={upload}
          conversations={conversations}
          onFileSelect={handleFileSelect}
          onScopeChange={(scope) =>
            setUpload((u) => ({ ...u, scope, error: null }))
          }
          onConversationChange={(cid) =>
            setUpload((u) => ({ ...u, conversationId: cid, error: null }))
          }
          onStart={startUpload}
          onClose={closeUpload}
        />
      )}

      {/* Scope dialog (public → private) */}
      {scopeDialog && (
        <ScopeDialog
          doc={scopeDialog}
          conversations={conversations}
          onConfirm={confirmScopeDialog}
          onCancel={() => setScopeDialog(null)}
        />
      )}
    </div>
  );
}
