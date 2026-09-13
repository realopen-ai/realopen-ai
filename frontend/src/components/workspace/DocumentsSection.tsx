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
  ArrowDownUp,
  Files,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
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

// ─── Helpers ─────────────────────────────────────────────────────────

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

const ACCEPTED_EXTS =
  ".txt,.md,.markdown,.csv,.tsv,.pdf,.docx,.doc,.xlsx,.xls,.pptx,.ppt";

function fileExt(name: string): string {
  return name.split(".").pop()?.toLowerCase() || "";
}

// ─── Type groups (filter pills, same style as Generated Files) ───────

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
  color: string;
  bg: string;
} {
  const ext = fileExt(doc.filename);
  for (const g of TYPE_GROUPS) {
    if (g.exts.includes(ext)) {
      switch (g.key) {
        case "pdf":
          return {
            key: "pdf",
            icon: FileText,
            color: "text-red-400",
            bg: "bg-red-500/10",
          };
        case "word":
          return {
            key: "word",
            icon: FileText,
            color: "text-blue-400",
            bg: "bg-blue-500/10",
          };
        case "excel":
          return {
            key: "excel",
            icon: FileSpreadsheet,
            color: "text-emerald-400",
            bg: "bg-emerald-500/10",
          };
        case "slides":
          return {
            key: "slides",
            icon: Presentation,
            color: "text-amber-400",
            bg: "bg-amber-500/10",
          };
        case "text":
          return {
            key: "text",
            icon: FileText,
            color: "text-slate-400",
            bg: "bg-slate-500/10",
          };
        case "data":
          return {
            key: "data",
            icon: Table,
            color: "text-violet-400",
            bg: "bg-violet-500/10",
          };
      }
    }
  }
  return {
    key: "other",
    icon: FileText,
    color: "text-muted-foreground",
    bg: "bg-secondary",
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
      <span className="inline-flex items-center gap-1 text-[11px] text-emerald-400">
        <Check className="w-3 h-3" /> RAG: Indexed
      </span>
    );
  }
  if (doc.digestion_status === "digesting") {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-blue-400">
        <Loader2 className="w-3 h-3 animate-spin" /> RAG: Digesting…
      </span>
    );
  }
  if (doc.digestion_status === "failed") {
    return (
      <span
        className="inline-flex items-center gap-1 text-[11px] text-red-400"
        title={doc.digestion_error ?? ""}
      >
        <AlertCircle className="w-3 h-3" /> RAG: Failed
      </span>
    );
  }
  if (doc.digestion_status === "not_indexed") {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
        <X className="w-3 h-3" /> RAG: Not indexed
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
      <Loader2 className="w-3 h-3 animate-spin" /> RAG: Pending
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
  const t = typeOf(doc);
  const Icon = t.icon;

  return (
    <div
      className={cn(
        "group relative rounded-xl border border-border bg-secondary/20 hover:bg-accent/20 hover:border-primary/30 transition-all overflow-hidden cursor-pointer",
        busy && "opacity-60 pointer-events-none",
      )}
      onClick={onOpen}
    >
      {/* Thumbnail area */}
      <div className="relative aspect-4/3 bg-secondary/40 flex items-center justify-center overflow-hidden">
        {thumbError ? (
          <div
            className={cn(
              "w-12 h-12 rounded-xl flex items-center justify-center",
              t.bg,
            )}
          >
            <Icon className={cn("w-6 h-6", t.color)} />
          </div>
        ) : (
          <img
            src={documentThumbnailUrl(doc.id)}
            alt={`${doc.filename} preview`}
            loading="lazy"
            onError={() => setThumbError(true)}
            className="w-full h-full object-cover object-top"
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
              ? "Public — all conversations can search this. Click to make private."
              : "Private — only the attached conversation can search this. Click to make public."
          }
          className={cn(
            "absolute top-2 right-2 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10.5px] font-medium backdrop-blur-sm transition-colors",
            doc.scope === "public"
              ? "bg-emerald-500/80 text-white hover:bg-emerald-600"
              : "bg-blue-500/80 text-white hover:bg-blue-600",
          )}
        >
          {doc.scope === "public" ? (
            <>
              <Globe className="w-2.5 h-2.5" /> Public
            </>
          ) : (
            <>
              <Lock className="w-2.5 h-2.5" /> Private
            </>
          )}
        </button>

        {/* Quick actions (hover on desktop) */}
        <div className="absolute top-2 left-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDownload();
            }}
            title="Download"
            className="w-7 h-7 rounded-lg bg-background/80 backdrop-blur-sm flex items-center justify-center text-muted-foreground hover:text-foreground transition-colors"
          >
            <Download className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            title="Delete"
            className="w-7 h-7 rounded-lg bg-background/80 backdrop-blur-sm flex items-center justify-center text-muted-foreground hover:text-red-400 transition-colors"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Info */}
      <div className="p-3">
        <p
          className="text-[13px] font-medium text-foreground truncate"
          title={doc.original_filename}
        >
          {doc.filename}
        </p>
        <div className="mt-1.5 flex flex-col gap-0.5">
          <RagStatus doc={doc} />
          <span className="text-[10.5px] text-muted-foreground/70">
            {formatSize(doc.file_size_bytes)}
          </span>
          <span className="text-[10.5px] text-muted-foreground/70">
            {formatDate(doc.created_at)}
          </span>
        </div>
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
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-xl border border-border/60 bg-background shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border/40">
          <h2 className="text-[14px] font-semibold">Upload document</h2>
          <button
            onClick={onClose}
            disabled={isUploading}
            className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-secondary disabled:opacity-40"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="px-4 py-4 space-y-3.5 max-h-[70vh] overflow-y-auto">
          {/* File picker */}
          <div>
            <label className="text-[11.5px] text-muted-foreground mb-1.5 block">
              File
            </label>
            <input
              type="file"
              accept={ACCEPTED_EXTS}
              onChange={onFileSelect}
              disabled={isUploading}
              className="w-full text-[12px] file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:bg-primary file:text-primary-foreground file:text-[12px] file:font-medium file:cursor-pointer file:hover:bg-primary/90"
            />
            <p className="text-[10.5px] text-muted-foreground/60 mt-1">
              Accepted: PDF, DOCX, XLSX, PPTX, TXT, MD, CSV (and DOC, XLS, PPT,
              TSV)
            </p>
          </div>

          {/* Scope radio */}
          <div>
            <label className="text-[11.5px] text-muted-foreground mb-1.5 block">
              Scope
            </label>
            <div className="grid grid-cols-2 gap-2">
              <button
                onClick={() => onScopeChange("public")}
                disabled={isUploading}
                className={cn(
                  "flex flex-col items-start gap-0.5 px-3 py-2 rounded-md border text-left transition-colors",
                  scope === "public"
                    ? "border-emerald-500/60 bg-emerald-500/5"
                    : "border-border/40 hover:bg-secondary/30",
                )}
              >
                <div className="flex items-center gap-1.5">
                  <Globe className="w-3.5 h-3.5 text-emerald-400" />
                  <span className="text-[12px] font-medium">Public</span>
                </div>
                <span className="text-[10.5px] text-muted-foreground/70">
                  Searchable by all conversations
                </span>
              </button>
              <button
                onClick={() => onScopeChange("private")}
                disabled={isUploading}
                className={cn(
                  "flex flex-col items-start gap-0.5 px-3 py-2 rounded-md border text-left transition-colors",
                  scope === "private"
                    ? "border-blue-500/60 bg-blue-500/5"
                    : "border-border/40 hover:bg-secondary/30",
                )}
              >
                <div className="flex items-center gap-1.5">
                  <Lock className="w-3.5 h-3.5 text-blue-400" />
                  <span className="text-[12px] font-medium">Private</span>
                </div>
                <span className="text-[10.5px] text-muted-foreground/70">
                  Only one conversation
                </span>
              </button>
            </div>
          </div>

          {/* Conversation picker (only for private) */}
          {scope === "private" && (
            <div>
              <label className="text-[11.5px] text-muted-foreground mb-1.5 block">
                Attach to conversation
              </label>
              {conversations.length === 0 ? (
                <div className="text-[11.5px] text-amber-400/80 flex items-center gap-1.5">
                  <AlertCircle className="w-3.5 h-3.5" />
                  No conversations yet — create one in chat first.
                </div>
              ) : (
                <select
                  value={conversationId}
                  onChange={(e) => onConversationChange(e.target.value)}
                  disabled={isUploading}
                  className="w-full text-[12px] py-1.5 px-2 rounded-md bg-secondary/50 border border-border/40 focus:outline-none focus:border-primary/50"
                >
                  <option value="">— Pick a conversation —</option>
                  {conversations.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.title || "Untitled"} ·{" "}
                      {new Date(c.createdAt).toLocaleDateString()}
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}

          {/* Progress bar */}
          {progress && (
            <div className="rounded-md border border-border/40 bg-secondary/20 px-3 py-2">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[11px] font-medium text-foreground">
                  {progress.stage === "done"
                    ? "Done"
                    : progress.stage === "error"
                      ? "Error"
                      : progress.stage.replace(/_/g, " ")}
                </span>
                <span className="text-[11px] text-muted-foreground/70">
                  {progress.percent}%
                </span>
              </div>
              <div className="h-1.5 rounded-full bg-secondary/70 overflow-hidden">
                <div
                  className={cn(
                    "h-full transition-all",
                    progress.stage === "error"
                      ? "bg-red-500"
                      : progress.stage === "done"
                        ? "bg-emerald-500"
                        : "bg-primary",
                  )}
                  style={{ width: `${progress.percent}%` }}
                />
              </div>
              {progress.details && (
                <p className="text-[10.5px] text-muted-foreground/70 mt-1">
                  {progress.details}
                </p>
              )}
            </div>
          )}

          {/* Done badge */}
          {doneDoc && (
            <div className="rounded-md border border-emerald-500/40 bg-emerald-500/5 px-3 py-2 flex items-start gap-2">
              <Check className="w-4 h-4 text-emerald-400 mt-0.5 shrink-0" />
              <div className="text-[11.5px]">
                <p className="font-medium text-emerald-400">
                  Document ready for RAG
                </p>
                <p className="text-muted-foreground/80">
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
            <div className="rounded-md border border-red-500/40 bg-red-500/5 px-3 py-2 flex items-start gap-2">
              <AlertCircle className="w-4 h-4 text-red-400 mt-0.5 shrink-0" />
              <p className="text-[11.5px] text-red-400">{error}</p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-border/40">
          <button
            onClick={onClose}
            disabled={isUploading}
            className="px-3 py-1.5 text-[12px] font-medium rounded-md hover:bg-secondary disabled:opacity-40"
          >
            {doneDoc ? "Close" : "Cancel"}
          </button>
          {!doneDoc && (
            <button
              onClick={onStart}
              disabled={
                isUploading || !file || (scope === "private" && !conversationId)
              }
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
            >
              {isUploading ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  Uploading...
                </>
              ) : (
                <>
                  <Upload className="w-3.5 h-3.5" />
                  Upload & digest
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
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
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={onCancel}
    >
      <div
        className="w-full max-w-sm rounded-xl border border-border/60 bg-background shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-border/40">
          <h2 className="text-[14px] font-semibold">Make private</h2>
          <button
            onClick={onCancel}
            className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-secondary"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="px-4 py-4 space-y-3">
          <p className="text-[12px] text-muted-foreground">
            Attach{" "}
            <span className="font-medium text-foreground">{doc.filename}</span>{" "}
            to a conversation — only that conversation will be able to search
            it.
          </p>
          {conversations.length === 0 ? (
            <div className="text-[11.5px] text-amber-400/80 flex items-center gap-1.5">
              <AlertCircle className="w-3.5 h-3.5" />
              No conversations yet — create one in chat first.
            </div>
          ) : (
            <select
              value={cid}
              onChange={(e) => setCid(e.target.value)}
              className="w-full text-[12px] py-1.5 px-2 rounded-md bg-secondary/50 border border-border/40 focus:outline-none focus:border-primary/50"
            >
              <option value="">— Pick a conversation —</option>
              {conversations.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.title || "Untitled"} ·{" "}
                  {new Date(c.createdAt).toLocaleDateString()}
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-border/40">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 text-[12px] font-medium rounded-md hover:bg-secondary"
          >
            Cancel
          </button>
          <button
            onClick={() => cid && onConfirm(cid)}
            disabled={!cid}
            className="px-3 py-1.5 text-[12px] font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
          >
            Make private
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── DocumentsSection ─────────────────────────────────────────────────

export function DocumentsSection() {
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
        error: "Pick a conversation to attach this private document to.",
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
        `Delete "${doc.filename}"? This removes the file and all its chunks.`,
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
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="px-4 pt-4 pb-2 space-y-2.5 border-b border-border/40">
        <div className="flex items-center gap-2 flex-wrap">
          {/* Search */}
          <div className="relative flex-1 min-w-45">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground/60" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search documents, collections…"
              className="w-full pl-8 pr-3 py-1.5 text-[12px] rounded-md bg-secondary/50 border border-border/40 focus:outline-none focus:border-primary/50"
            />
          </div>

          {/* Scope filter */}
          <select
            value={filterScope}
            onChange={(e) =>
              setFilterScope(e.target.value as "all" | DocumentScope)
            }
            className="text-[12px] py-1.5 px-2 rounded-md bg-secondary/50 border border-border/40 focus:outline-none"
            title="Filter by scope"
          >
            <option value="all">All scopes</option>
            <option value="public">Public</option>
            <option value="private">Private</option>
          </select>

          {/* Sort */}
          <div className="relative">
            <ArrowDownUp className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3.5 text-muted-foreground/60 pointer-events-none" />
            <select
              value={sortKey}
              onChange={(e) => setSortKey(e.target.value as SortKey)}
              className="pl-7 pr-2 py-1.5 text-[12px] rounded-md bg-secondary/50 border border-border/40 focus:outline-none appearance-none cursor-pointer"
              title="Sort order"
            >
              {SORT_OPTIONS.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>

          <div className="flex-1" />

          <div className="text-[11px] text-muted-foreground/60">
            {filtered.length} doc{filtered.length !== 1 ? "s" : ""}
          </div>

          <button
            onClick={openUpload}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
          >
            <Upload className="w-3.5 h-3.5" />
            Upload
          </button>
        </div>

        {/* Type filter pills (same style as Generated Files) */}
        <div className="flex gap-1.5 flex-wrap">
          {TYPE_GROUPS.map((g) => (
            <button
              key={g.key}
              onClick={() => setFilterType(g.key)}
              className={cn(
                "px-3 py-1.5 rounded-full text-[12px] font-medium transition-colors",
                filterType === g.key
                  ? "bg-primary/10 text-primary ring-1 ring-primary/20"
                  : "bg-secondary text-muted-foreground hover:text-foreground",
              )}
            >
              {g.label}
            </button>
          ))}
        </div>
      </div>

      {/* Document card grid */}
      <ScrollArea className="flex-1">
        <div className="p-4">
          {loading ? (
            <div className="flex flex-col items-center justify-center py-12 text-muted-foreground/60">
              <Loader2 className="w-5 h-5 animate-spin mb-2" />
              <p className="text-[12px]">Loading documents…</p>
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <div className="w-14 h-14 rounded-2xl bg-primary/10 flex items-center justify-center mb-3">
                {hasActiveFilters ? (
                  <FolderOpen className="w-7 h-7 text-primary/60" />
                ) : (
                  <Files className="w-7 h-7 text-primary/60" />
                )}
              </div>
              <p className="text-[13px] font-medium text-muted-foreground/70 mb-1">
                {hasActiveFilters
                  ? "No documents match your filters"
                  : "No documents yet"}
              </p>
              <p className="text-[11.5px] text-muted-foreground/50 max-w-70">
                {hasActiveFilters
                  ? "Try adjusting your search, scope or type filters."
                  : "Upload a PDF, DOCX, XLSX, PPTX, TXT, MD or CSV file to make it searchable by the AI."}
              </p>
              {!hasActiveFilters && (
                <button
                  onClick={openUpload}
                  className="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
                >
                  <Upload className="w-3.5 h-3.5" />
                  Upload your first document
                </button>
              )}
            </div>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3.5 max-w-6xl">
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
