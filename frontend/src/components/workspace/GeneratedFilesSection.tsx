import { useState, useEffect, useCallback } from "react";
import {
  FileText,
  Image as ImageIcon,
  FileType,
  Download,
  ExternalLink,
  Loader2,
  Eye,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { isLibreOfficeInstalled } from "@/api/depsClient";
import { PptxViewerModal } from "@/components/chat/PptxViewerModal";
import { cn } from "@/lib/utils";

interface GeneratedFile {
  filename: string;
  file_type: string;
  deliverable_type: string;
  download_url: string;
  report_id: string | null;
  created_at: number;
  message_id: string | null;
  conversation_id: string | null;
  conversation_title: string | null;
  file_size: number | null;
  original_prompt: string | null;
  thumbnail_url: string | null;
}

const fileTypeConfig: Record<
  string,
  { icon: typeof FileText; color: string; bg: string; label: string }
> = {
  pdf: {
    icon: FileText,
    color: "text-red-400",
    bg: "bg-red-500/10",
    label: "PDF",
  },
  docx: {
    icon: FileText,
    color: "text-blue-400",
    bg: "bg-blue-500/10",
    label: "DOCX",
  },
  pptx: {
    icon: FileType,
    color: "text-amber-400",
    bg: "bg-amber-500/10",
    label: "PPTX",
  },
  image: {
    icon: ImageIcon,
    color: "text-emerald-400",
    bg: "bg-emerald-500/10",
    label: "Image",
  },
};

function formatFileSize(bytes: number | null): string {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(epoch: number): { date: string; time: string } {
  if (!epoch) return { date: "—", time: "—" };
  const d = new Date(epoch * 1000);
  return {
    date: d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    }),
    time: d.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    }),
  };
}

function getToolName(deliverableType: string): string {
  if (deliverableType === "presentation") return "use_pptx_gen";
  if (deliverableType === "report") return "use_report_gen";
  if (deliverableType === "image") return "use_image_gen";
  return deliverableType;
}

/** Thumbnail with onError fallback to a file-type icon.
 */
function FileThumbnail({
  fileType,
  thumbnailUrl,
}: {
  fileType: string;
  thumbnailUrl: string | null;
}) {
  const [thumbError, setThumbError] = useState(false);
  const cfg = fileTypeConfig[fileType] ?? fileTypeConfig.image;
  const Icon = cfg.icon;
  const showThumb = thumbnailUrl && !thumbError;

  if (showThumb) {
    return (
      <div className="w-14 h-10 rounded-lg shrink-0 overflow-hidden bg-secondary/50 ring-1 ring-border">
        <img
          src={thumbnailUrl!}
          alt="thumbnail"
          onError={() => setThumbError(true)}
          className="w-full h-full object-cover"
          loading="lazy"
        />
      </div>
    );
  }

  return (
    <div
      className={cn(
        "w-10 h-10 rounded-lg flex items-center justify-center shrink-0",
        cfg.bg,
      )}
    >
      <Icon className={cn("w-5 h-5", cfg.color)} />
    </div>
  );
}

export function GeneratedFilesSection({
  onOpenConversation,
}: {
  onOpenConversation: (conversationId: string) => void;
}) {
  const [files, setFiles] = useState<GeneratedFile[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [filter, setFilter] = useState<string>("all");
  const [libreOfficeAvailable, setLibreOfficeAvailable] = useState(false);
  const [viewingPptx, setViewingPptx] = useState<{
    reportId: string;
    filename: string;
    downloadUrl: string;
  } | null>(null);

  // Check if LibreOffice is installed (for PPTX view feature)
  useEffect(() => {
    isLibreOfficeInstalled().then(setLibreOfficeAvailable);
  }, []);

  const loadFiles = useCallback(async () => {
    setIsLoading(true);
    try {
      const params = new URLSearchParams();
      if (filter !== "all") params.set("file_type", filter);
      params.set("limit", "100");
      const res = await fetch(
        `/api/workspace/generated-files?${params.toString()}`,
      );
      if (res.ok) {
        const data = await res.json();
        setFiles(data.files ?? []);
      }
    } catch {
      /* ignore */
    }
    setIsLoading(false);
  }, [filter]);

  useEffect(() => {
    loadFiles();
  }, [loadFiles]);

  const filterButtons = [
    { key: "all", label: "All" },
    { key: "pdf", label: "PDF" },
    { key: "docx", label: "DOCX" },
    { key: "pptx", label: "PPTX" },
  ];

  return (
    <div className="flex flex-col h-full">
      {/* Filter bar */}
      <div className="flex items-center gap-2 px-4 pt-4 pb-3">
        <div className="flex gap-1.5">
          {filterButtons.map((btn) => (
            <button
              key={btn.key}
              onClick={() => setFilter(btn.key)}
              className={cn(
                "px-3 py-1.5 rounded-full text-[12px] font-medium transition-colors",
                filter === btn.key
                  ? "bg-primary/10 text-primary ring-1 ring-primary/20"
                  : "bg-secondary text-muted-foreground hover:text-foreground",
              )}
            >
              {btn.label}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        <p className="text-[12px] text-muted-foreground/60">
          {files.length} file(s)
        </p>
      </div>

      {/* File list */}
      <ScrollArea className="flex-1 px-4">
        <div className="pb-4">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-5 h-5 text-primary animate-spin" />
            </div>
          ) : files.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12">
              <FileText className="w-8 h-8 text-muted-foreground/20 mb-2" />
              <p className="text-[13px] text-muted-foreground/60">
                No generated files yet.
              </p>
              <p className="text-[11px] text-muted-foreground/40 mt-1">
                Generate a report or presentation in chat to see files here.
              </p>
            </div>
          ) : (
            <div className="space-y-1.5">
              {files.map((file, i) => {
                const { date, time } = formatDate(file.created_at);
                return (
                  <div
                    key={i}
                    className="flex items-center gap-3 p-3 rounded-xl border border-border hover:bg-accent/20 transition-colors"
                  >
                    <FileThumbnail
                      fileType={file.file_type}
                      thumbnailUrl={file.thumbnail_url}
                    />

                    {/* File info */}
                    <div className="flex-1 min-w-0">
                      <p className="text-[13px] font-medium text-foreground truncate">
                        {file.filename}
                      </p>
                      <div className="flex items-center gap-3 mt-0.5">
                        <span className="text-[11px] text-muted-foreground/60">
                          {date} · {time}
                        </span>
                        <span className="text-[11px] text-muted-foreground/60">
                          {formatFileSize(file.file_size)}
                        </span>
                        <span className="text-[11px] text-muted-foreground/60">
                          {getToolName(file.deliverable_type)}
                        </span>
                      </div>
                      {file.conversation_title && (
                        <p className="text-[10px] text-muted-foreground/40 truncate mt-0.5">
                          From: {file.conversation_title}
                        </p>
                      )}
                    </div>

                    {/* Actions */}
                    <div className="flex items-center gap-1 shrink-0">
                      {file.conversation_id && (
                        <button
                          onClick={() =>
                            file.conversation_id &&
                            onOpenConversation(file.conversation_id)
                          }
                          className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors"
                          title="Open conversation"
                        >
                          <ExternalLink className="w-4 h-4" />
                        </button>
                      )}
                      {libreOfficeAvailable &&
                        file.file_type === "pptx" &&
                        file.report_id && (
                          <button
                            onClick={() =>
                              setViewingPptx({
                                reportId: file.report_id!,
                                filename: file.filename,
                                downloadUrl: file.download_url,
                              })
                            }
                            className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors"
                            title="View presentation"
                          >
                            <Eye className="w-4 h-4" />
                          </button>
                        )}
                      <a
                        href={file.download_url}
                        download={file.filename}
                        className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
                        title="Download"
                      >
                        <Download className="w-4 h-4" />
                      </a>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </ScrollArea>

      {/* PPTX Viewer Modal */}
      {viewingPptx && (
        <PptxViewerModal
          reportId={viewingPptx.reportId}
          filename={viewingPptx.filename}
          downloadUrl={viewingPptx.downloadUrl}
          onClose={() => setViewingPptx(null)}
        />
      )}
    </div>
  );
}
