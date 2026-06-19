import { useState, useEffect, useRef, useCallback } from "react";
import {
  Upload,
  Search,
  Download,
  Trash2,
  Pencil,
  Globe,
  Lock,
  FileText,
  X,
  Check,
  Loader2,
  AlertCircle,
  Image as ImageIcon,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import {
  listDocuments,
  uploadDocumentStream,
  downloadDocument,
  deleteDocument,
  updateDocument,
  type DocumentDTO,
  type DocumentScope,
  type DigestProgress,
} from "@/api/documentsClient";
import { fetchConversations, type ConversationDTO } from "@/api/client";

// ─── Helpers ─────────────────────────────────────────────────────────

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

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

const ACCEPTED_EXTS = ".txt,.md,.markdown,.csv,.tsv,.pdf,.docx,.doc,.xlsx,.xls";

// ─── Upload Dialog ───────────────────────────────────────────────────

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

// ─── DocumentsTab ────────────────────────────────────────────────────

export function DocumentsTab() {
  const [docs, setDocs] = useState<DocumentDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [filterScope, setFilterScope] = useState<"all" | DocumentScope>("all");
  const [conversations, setConversations] = useState<ConversationDTO[]>([]);
  const [upload, setUpload] = useState<UploadState>(initialUpload);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    const list = await listDocuments();
    setDocs(list);
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Load conversations for the upload dialog picker
  useEffect(() => {
    if (upload.open && conversations.length === 0) {
      fetchConversations(100, 0)
        .then(setConversations)
        .catch(() => {});
    }
  }, [upload.open, conversations.length]);

  // ── Filtered docs based on search + scope filter ─────────────────
  const filtered = docs.filter((d) => {
    if (filterScope !== "all" && d.scope !== filterScope) return false;
    if (
      search &&
      !d.filename.toLowerCase().includes(search.toLowerCase()) &&
      !d.original_filename.toLowerCase().includes(search.toLowerCase())
    ) {
      return false;
    }
    return true;
  });

  // ── Upload handlers ──────────────────────────────────────────────
  const openUpload = () => {
    setUpload({ ...initialUpload, open: true });
  };

  const closeUpload = () => {
    if (upload.isUploading) return; // don't allow closing mid-upload
    setUpload(initialUpload);
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) {
      setUpload((u) => ({ ...u, file: f, error: null, doneDoc: null }));
    }
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
      onProgress: (p) => {
        setUpload((u) => ({ ...u, progress: p }));
      },
      onDone: (doc) => {
        setUpload((u) => ({
          ...u,
          isUploading: false,
          doneDoc: doc,
        }));
        refresh(); // refresh list
      },
      onError: (err) => {
        setUpload((u) => ({
          ...u,
          isUploading: false,
          error: err,
        }));
      },
    });
  };

  // ── Rename / delete / toggle ─────────────────────────────────────
  const handleRename = async (doc: DocumentDTO) => {
    const newName = editingName.trim();
    if (!newName || newName === doc.filename) {
      setEditingId(null);
      return;
    }
    setBusyId(doc.id);
    await updateDocument(doc.id, { filename: newName });
    setBusyId(null);
    setEditingId(null);
    refresh();
  };

  const handleDelete = async (doc: DocumentDTO) => {
    if (
      !confirm(
        `Delete "${doc.filename}"? This removes the file and all its chunks.`,
      )
    ) {
      return;
    }
    setBusyId(doc.id);
    await deleteDocument(doc.id);
    setBusyId(null);
    refresh();
  };

  const handleToggleScope = async (doc: DocumentDTO) => {
    const newScope: DocumentScope =
      doc.scope === "public" ? "private" : "public";
    let newConvId: string | null = null;
    if (newScope === "private") {
      // Prompt user to pick a conversation
      const convList = await fetchConversations(100, 0).catch(() => []);
      if (convList.length === 0) {
        alert("You need at least one conversation to attach a private doc to.");
        return;
      }
      const choice = prompt(
        `Attach "${doc.filename}" to which conversation?\n\n` +
          convList
            .map(
              (c, i) =>
                `${i + 1}. ${c.title || "Untitled"} (${c.id.slice(0, 8)})`,
            )
            .join("\n") +
          "\n\nEnter the number:",
      );
      if (!choice) return;
      const idx = parseInt(choice, 10) - 1;
      if (isNaN(idx) || idx < 0 || idx >= convList.length) {
        alert("Invalid choice.");
        return;
      }
      newConvId = convList[idx].id;
    }
    setBusyId(doc.id);
    await updateDocument(doc.id, {
      scope: newScope,
      conversation_id: newConvId,
    });
    setBusyId(null);
    refresh();
  };

  // ─── Render ─────────────────────────────────────────────────────
  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-4 py-3 border-b border-border/40">
        <div className="relative flex-1 max-w-65">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground/60" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search documents..."
            className="w-full pl-8 pr-3 py-1.5 text-[12px] rounded-md bg-secondary/50 border border-border/40 focus:outline-none focus:border-primary/50"
          />
        </div>
        <select
          value={filterScope}
          onChange={(e) =>
            setFilterScope(e.target.value as "all" | DocumentScope)
          }
          className="text-[12px] py-1.5 px-2 rounded-md bg-secondary/50 border border-border/40 focus:outline-none"
        >
          <option value="all">All scopes</option>
          <option value="public">Public</option>
          <option value="private">Private</option>
        </select>
        <div className="text-[11px] text-muted-foreground/60">
          {filtered.length} doc{filtered.length !== 1 ? "s" : ""}
        </div>
        <button
          onClick={openUpload}
          className="ml-auto inline-flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          <Upload className="w-3.5 h-3.5" />
          Upload
        </button>
      </div>

      {/* List */}
      <ScrollArea className="flex-1">
        <div className="px-4 py-3">
          {loading ? (
            <div className="flex flex-col items-center justify-center py-12 text-muted-foreground/60">
              <Loader2 className="w-5 h-5 animate-spin mb-2" />
              <p className="text-[12px]">Loading documents...</p>
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <FileText className="w-8 h-8 text-muted-foreground/20 mb-2" />
              <p className="text-[13px] font-medium text-muted-foreground/70 mb-1">
                {search || filterScope !== "all"
                  ? "No documents match your filters"
                  : "No documents yet"}
              </p>
              <p className="text-[11.5px] text-muted-foreground/50 max-w-70">
                {search || filterScope !== "all"
                  ? "Try adjusting your search or scope filter."
                  : "Upload a PDF, DOCX, XLSX, TXT, MD or CSV file to make it searchable by the AI."}
              </p>
              {!search && filterScope === "all" && (
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
            <div className="space-y-1.5">
              {filtered.map((doc) => (
                <DocumentRow
                  key={doc.id}
                  doc={doc}
                  busy={busyId === doc.id}
                  editing={editingId === doc.id}
                  editingName={editingName}
                  onEditingNameChange={setEditingName}
                  onStartRename={() => {
                    setEditingId(doc.id);
                    setEditingName(doc.filename);
                  }}
                  onConfirmRename={() => handleRename(doc)}
                  onCancelRename={() => setEditingId(null)}
                  onDelete={() => handleDelete(doc)}
                  onToggleScope={() => handleToggleScope(doc)}
                  onDownload={() => downloadDocument(doc)}
                />
              ))}
            </div>
          )}
        </div>
      </ScrollArea>

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
          fileInputRef={fileInputRef}
          acceptedExts={ACCEPTED_EXTS}
        />
      )}
    </div>
  );
}

// ─── Document Row ────────────────────────────────────────────────────

function DocumentRow({
  doc,
  busy,
  editing,
  editingName,
  onEditingNameChange,
  onStartRename,
  onConfirmRename,
  onCancelRename,
  onDelete,
  onToggleScope,
  onDownload,
}: {
  doc: DocumentDTO;
  busy: boolean;
  editing: boolean;
  editingName: string;
  onEditingNameChange: (v: string) => void;
  onStartRename: () => void;
  onConfirmRename: () => void;
  onCancelRename: () => void;
  onDelete: () => void;
  onToggleScope: () => void;
  onDownload: () => void;
}) {
  const statusBadge = (() => {
    if (doc.digestion_status === "ready") {
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-emerald-500/10 text-emerald-400">
          <Check className="w-2.5 h-2.5" /> Ready
        </span>
      );
    }
    if (doc.digestion_status === "digesting") {
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-blue-500/10 text-blue-400">
          <Loader2 className="w-2.5 h-2.5 animate-spin" /> Digesting
        </span>
      );
    }
    if (doc.digestion_status === "failed") {
      return (
        <span
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-red-500/10 text-red-400"
          title={doc.digestion_error ?? ""}
        >
          <AlertCircle className="w-2.5 h-2.5" /> Failed
        </span>
      );
    }
    return (
      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-muted/30 text-muted-foreground">
        Pending
      </span>
    );
  })();

  const ext = doc.filename.split(".").pop()?.toLowerCase() || "";
  const isImage = ["png", "jpg", "jpeg", "gif", "webp"].includes(ext);

  return (
    <div
      className={cn(
        "flex items-center gap-2.5 px-3 py-2 rounded-md border border-border/40 bg-secondary/20 hover:bg-secondary/40 transition-colors",
        busy && "opacity-60",
      )}
    >
      {/* Icon */}
      <div className="shrink-0">
        {isImage ? (
          <ImageIcon className="w-4 h-4 text-violet-400" />
        ) : (
          <FileText className="w-4 h-4 text-amber-400/80" />
        )}
      </div>

      {/* Name + meta */}
      <div className="flex-1 min-w-0">
        {editing ? (
          <input
            autoFocus
            value={editingName}
            onChange={(e) => onEditingNameChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onConfirmRename();
              if (e.key === "Escape") onCancelRename();
            }}
            className="w-full text-[12.5px] font-medium px-1.5 py-0.5 rounded bg-background border border-primary/50 focus:outline-none"
          />
        ) : (
          <div
            className="text-[12.5px] font-medium text-foreground truncate"
            title={doc.original_filename}
          >
            {doc.filename}
          </div>
        )}
        <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
          {statusBadge}
          <span className="text-[10.5px] text-muted-foreground/70">
            {formatSize(doc.file_size_bytes)}
          </span>
          <span className="text-[10.5px] text-muted-foreground/70">
            · {doc.total_chunks} chunk{doc.total_chunks !== 1 ? "s" : ""}
          </span>
          {doc.total_images > 0 && (
            <span className="text-[10.5px] text-muted-foreground/70">
              · {doc.total_images} img
            </span>
          )}
          <span className="text-[10.5px] text-muted-foreground/70">
            · {relativeTime(doc.created_at)}
          </span>
        </div>
      </div>

      {/* Scope badge */}
      <button
        onClick={onToggleScope}
        disabled={busy}
        title={
          doc.scope === "public"
            ? "Public — all conversations can search this. Click to make private."
            : "Private — only the attached conversation can search this. Click to make public."
        }
        className={cn(
          "shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10.5px] transition-colors",
          doc.scope === "public"
            ? "bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20"
            : "bg-blue-500/10 text-blue-400 hover:bg-blue-500/20",
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

      {/* Actions */}
      <div className="shrink-0 flex items-center gap-0.5">
        {editing ? (
          <>
            <button
              onClick={onConfirmRename}
              className="p-1 rounded text-emerald-400 hover:bg-emerald-500/10"
              title="Save name"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={onCancelRename}
              className="p-1 rounded text-muted-foreground hover:bg-secondary"
              title="Cancel"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </>
        ) : (
          <>
            <button
              onClick={onDownload}
              disabled={busy}
              className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-secondary disabled:opacity-40"
              title="Download"
            >
              <Download className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={onStartRename}
              disabled={busy}
              className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-secondary disabled:opacity-40"
              title="Rename"
            >
              <Pencil className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={onDelete}
              disabled={busy}
              className="p-1 rounded text-muted-foreground hover:text-red-400 hover:bg-red-500/10 disabled:opacity-40"
              title="Delete"
            >
              {busy ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Trash2 className="w-3.5 h-3.5" />
              )}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

// ─── Upload Dialog ───────────────────────────────────────────────────

function UploadDialog({
  state,
  conversations,
  onFileSelect,
  onScopeChange,
  onConversationChange,
  onStart,
  onClose,
  fileInputRef,
  acceptedExts,
}: {
  state: UploadState;
  conversations: ConversationDTO[];
  onFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onScopeChange: (scope: DocumentScope) => void;
  onConversationChange: (cid: string) => void;
  onStart: () => void;
  onClose: () => void;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  acceptedExts: string;
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
        <div className="px-4 py-4 space-y-3.5">
          {/* File picker */}
          <div>
            <label className="text-[11.5px] text-muted-foreground mb-1.5 block">
              File
            </label>
            <input
              ref={fileInputRef}
              type="file"
              accept={acceptedExts}
              onChange={onFileSelect}
              disabled={isUploading}
              className="w-full text-[12px] file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:bg-primary file:text-primary-foreground file:text-[12px] file:font-medium file:cursor-pointer file:hover:bg-primary/90"
            />
            <p className="text-[10.5px] text-muted-foreground/60 mt-1">
              Accepted: PDF, DOCX, XLSX, CSV, MD, TXT (and DOC, TSV)
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
