import { useState, useEffect, useCallback } from "react";
import {
  FileText,
  Image as ImageIcon,
  FileType,
  FileSpreadsheet,
  Download,
  ExternalLink,
  Loader2,
  Eye,
  FileImage,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { EmptyState, FilterChip } from "@/components/ui/primitives";
import { isLibreOfficeInstalled } from "@/api/depsClient";
import {
  FileViewerModal,
  type ViewerFormat,
} from "@/components/chat/FileViewerModal";
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
  { icon: typeof FileText; tint: string; label: string }
> = {
  pdf: {
    icon: FileText,
    tint: "bg-red-500/10 text-red-600 dark:text-red-400",
    label: "PDF",
  },
  docx: {
    icon: FileText,
    tint: "bg-blue-500/10 text-blue-600 dark:text-blue-400",
    label: "DOCX",
  },
  pptx: {
    icon: FileType,
    tint: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
    label: "PPTX",
  },
  xlsx: {
    icon: FileSpreadsheet,
    tint: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
    label: "XLSX",
  },
  image: {
    icon: ImageIcon,
    tint: "bg-purple-500/10 text-purple-600 dark:text-purple-400",
    label: "Image",
  },
};

function formatFileSize(bytes: number | null): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(epoch: number): string {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

const deliverableLabels: Record<string, string> = {
  presentation: "Presentation Generation",
  report: "Report Generation",
  excel: "Excel Generation",
  image: "Image Generation",
};

function deliverableLabel(deliverableType: string): string {
  return deliverableLabels[deliverableType] ?? deliverableType;
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
      <div className="h-10 w-14 shrink-0 overflow-hidden rounded-lg bg-secondary ring-1 ring-border/60">
        <img
          src={thumbnailUrl!}
          alt="thumbnail"
          onError={() => setThumbError(true)}
          className="h-full w-full object-cover"
          loading="lazy"
        />
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg",
        cfg.tint,
      )}
    >
      <Icon className="h-5 w-5" />
    </div>
  );
}

const rowActionClass =
  "flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60";

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
    format: ViewerFormat;
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
    { key: "xlsx", label: "XLSX" },
  ];

  return (
    <div className="flex h-full flex-col">
      {/* Filter bar */}
      <div className="shrink-0 px-6 pt-5 lg:px-10">
        <div className="mx-auto flex w-full max-w-300 flex-wrap items-center gap-2">
          <div className="flex flex-wrap gap-1">
            {filterButtons.map((btn) => (
              <FilterChip
                key={btn.key}
                active={filter === btn.key}
                onClick={() => setFilter(btn.key)}
              >
                {btn.label}
              </FilterChip>
            ))}
          </div>
          <div className="flex-1" />
          <p className="text-xs text-muted-foreground">
            {files.length} file{files.length !== 1 ? "s" : ""}
          </p>
        </div>
      </div>

      {/* File list */}
      <ScrollArea className="min-h-0 flex-1">
        <div className="mx-auto w-full max-w-300 px-6 pb-16 pt-4 lg:px-10">
          {isLoading ? (
            <div className="flex items-center justify-center py-16 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
            </div>
          ) : files.length === 0 ? (
            <EmptyState
              icon={<FileImage />}
              title="No generated files yet"
              description="Generate a report, presentation, or spreadsheet in chat to see files here."
              className="rounded-xl"
            />
          ) : (
            <div className="overflow-hidden rounded-xl border border-border/60 bg-card">
              <ul className="divide-y divide-border/50">
                {files.map((file, i) => {
                  const date = formatDate(file.created_at);
                  const size = formatFileSize(file.file_size);
                  return (
                    <li
                      key={i}
                      className="group flex items-center gap-4 px-4 py-3 transition-colors hover:bg-surface-hover"
                    >
                      <FileThumbnail
                        fileType={file.file_type}
                        thumbnailUrl={file.thumbnail_url}
                      />

                      {/* File info */}
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[13.5px] font-medium text-foreground">
                          {file.filename}
                        </p>
                        <p className="mt-0.5 truncate text-xs text-muted-foreground">
                          {date} · {deliverableLabel(file.deliverable_type)}
                          {size ? ` · ${size}` : ""}
                        </p>
                        {file.conversation_title && (
                          <p className="mt-0.5 truncate text-xs text-muted-foreground/80">
                            From: {file.conversation_title}
                          </p>
                        )}
                      </div>

                      {/* Actions — quiet until hover */}
                      <div className="flex shrink-0 items-center gap-0.5 opacity-60 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                        {file.conversation_id && (
                          <button
                            onClick={() =>
                              file.conversation_id &&
                              onOpenConversation(file.conversation_id)
                            }
                            className={rowActionClass}
                            title="Open conversation"
                            aria-label={`Open conversation for ${file.filename}`}
                          >
                            <ExternalLink className="h-4 w-4" />
                          </button>
                        )}
                        {file.report_id &&
                          (file.file_type === "pdf" || libreOfficeAvailable) &&
                          ["pdf", "docx", "pptx", "xlsx"].includes(
                            file.file_type,
                          ) && (
                            <button
                              onClick={() =>
                                setViewingPptx({
                                  reportId: file.report_id!,
                                  filename: file.filename,
                                  downloadUrl: file.download_url,
                                  format: file.file_type as ViewerFormat,
                                })
                              }
                              className={rowActionClass}
                              title={
                                file.file_type === "pptx"
                                  ? "View presentation"
                                  : file.file_type === "xlsx"
                                    ? "View spreadsheet"
                                    : "View document"
                              }
                              aria-label={`View ${file.filename}`}
                            >
                              <Eye className="h-4 w-4" />
                            </button>
                          )}
                        <a
                          href={file.download_url}
                          download={file.filename}
                          className={rowActionClass}
                          title="Download"
                          aria-label={`Download ${file.filename}`}
                        >
                          <Download className="h-4 w-4" />
                        </a>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      </ScrollArea>

      {/* Document Viewer Modal (PPTX / PDF / DOCX / XLSX) */}
      {viewingPptx && (
        <FileViewerModal
          reportId={viewingPptx.reportId}
          filename={viewingPptx.filename}
          downloadUrl={viewingPptx.downloadUrl}
          format={viewingPptx.format}
          onClose={() => setViewingPptx(null)}
        />
      )}
    </div>
  );
}
