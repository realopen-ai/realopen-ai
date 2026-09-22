import { useState, useRef, useCallback, useEffect } from "react";
import {
  Search,
  Code2,
  Zap,
  ChevronDown,
  ChevronRight,
  Globe,
  FileCode,
  Brain,
  Eye,
  Copy,
  Volume2,
  RefreshCw,
  Check,
  Loader2,
  Image,
  FileText,
  FileSpreadsheet,
  Quote,
  AlertCircle,
  Download,
  FileType,
  Mic,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Message, ToolCallResult, MessageBlock } from "@/store/chatStore";
import { useChatStore } from "@/store/chatStore";
import type { RetrievedSourceDTO } from "@/api/documentsClient";
import { isLibreOfficeInstalled } from "@/api/depsClient";
import { t } from "@/store/settingsStore";
import {
  FileViewerModal,
  type ViewerFormat,
} from "@/components/chat/FileViewerModal";
import { cn } from "@/lib/utils";

// ─── Source Cards (RAG citations — rendered inside a tool_call block) ──

function SourceImage({ chunkId }: { chunkId: string }) {
  // Lazy-load the image only when the card is expanded. We use a simple
  // <img> tag — the browser handles caching via the Cache-Control header
  // the backend sets.
  const [loaded, setLoaded] = useState(false);
  const [errored, setErrored] = useState(false);
  if (errored) return null;
  return (
    <div className="mt-2 rounded-md overflow-hidden border border-border/30 bg-background/40 max-w-70">
      {!loaded && (
        <div className="w-full h-30 flex items-center justify-center text-[10px] text-muted-foreground/60">
          Loading image…
        </div>
      )}
      <img
        src={`/api/documents/chunks/${chunkId}/image`}
        alt="Document excerpt image"
        className={cn("w-full h-auto", loaded ? "block" : "hidden")}
        onLoad={() => setLoaded(true)}
        onError={() => setErrored(true)}
        loading="lazy"
      />
    </div>
  );
}

function SourceCards({ sources }: { sources: RetrievedSourceDTO[] }) {
  const [expanded, setExpanded] = useState(true);
  const [openIdx, setOpenIdx] = useState<number | null>(0);

  if (!sources.length) return null;

  return (
    <div className="mt-2 rounded-lg border border-border/60 bg-secondary/30 overflow-hidden">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-secondary/50 transition-colors"
      >
        {expanded ? (
          <ChevronDown className="w-3.5 h-3.5 text-muted-foreground" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 text-muted-foreground" />
        )}
        <Quote className="w-3.5 h-3.5 text-primary" />
        <span className="text-[12px] font-medium text-foreground">
          {sources.length} Source{sources.length !== 1 ? "s" : ""}
        </span>
        <span className="text-[11px] text-muted-foreground/70 ml-1">
          Retrieved from your documents
        </span>
      </button>

      {/* Cards */}
      {expanded && (
        <div className="px-2 pb-2 space-y-1.5">
          {sources.map((s, i) => {
            const isOpen = openIdx === i;
            const locationParts: string[] = [];
            if (s.page_number != null)
              locationParts.push(`p. ${s.page_number}`);
            if (s.line_start != null && s.line_end != null) {
              if (s.line_start === s.line_end) {
                locationParts.push(`L${s.line_start}`);
              } else {
                locationParts.push(`L${s.line_start}–${s.line_end}`);
              }
            }
            const location =
              locationParts.length > 0 ? ` · ${locationParts.join(", ")}` : "";
            const isImage = s.chunk_type === "image_description" && s.has_image;
            return (
              <div
                key={s.chunk_id}
                className="rounded-md border border-border/40 bg-background/60 overflow-hidden"
              >
                {/* Card header */}
                <button
                  onClick={() => setOpenIdx(isOpen ? null : i)}
                  className="w-full flex items-center gap-2 px-2.5 py-1.5 text-left hover:bg-secondary/40 transition-colors"
                >
                  {isOpen ? (
                    <ChevronDown className="w-3 h-3 text-muted-foreground shrink-0" />
                  ) : (
                    <ChevronRight className="w-3 h-3 text-muted-foreground shrink-0" />
                  )}
                  <FileText className="w-3 h-3 text-amber-400/80 shrink-0" />
                  <span
                    className="text-[11px] font-medium text-foreground truncate flex-1"
                    title={s.document_filename}
                  >
                    {s.document_filename}
                  </span>
                  {isImage && (
                    <Image className="w-3 h-3 text-violet-400 shrink-0" />
                  )}
                  <span className="text-[10px] text-muted-foreground/70 shrink-0">
                    {location}
                  </span>
                  <span className="text-[10px] text-muted-foreground/50 shrink-0">
                    {(s.score * 100).toFixed(0)}%
                  </span>
                </button>
                {/* Card body (snippet / full text + optional image) */}
                {isOpen && (
                  <div className="px-2.5 pb-2 pt-0.5">
                    <p className="text-[11.5px] leading-relaxed text-muted-foreground whitespace-pre-wrap">
                      {s.text}
                    </p>
                    {isImage && <SourceImage chunkId={s.chunk_id} />}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Digest Progress (RAG ingestion, shown inline on user message) ──

/**
 * Inline digestion progress indicator shown on the user's message
 * bubble when they uploaded documents via chat. The backend streams
 * `document_digest_*` SSE events while it extracts text, chunks,
 * describes images, and embeds — we render a compact progress bar
 * per file so the user sees real-time feedback in the conversation
 * itself, not just in the right-panel terminal.
 *
 * Visibility rules:
 *   - Only shown when message.digestProgress is non-empty.
 *   - Auto-hides a file's indicator 5 seconds after it reaches
 *     "done" or "error" so the conversation doesn't accumulate
 *     stale progress bars.
 */
function DigestProgressIndicator({
  items,
}: {
  items: NonNullable<Message["digestProgress"]>;
}) {
  if (!items.length) return null;

  // Group by filename so we show one card per file with the latest
  // progress for that file (not one card per SSE event).
  const byFile = new Map<string, (typeof items)[number]>();
  for (const item of items) {
    byFile.set(item.filename, item);
  }

  return (
    <div className="flex flex-col gap-1.5 mb-1.5 justify-end">
      {Array.from(byFile.entries()).map(([filename, p]) => {
        const isDone = p.stage === "done";
        const isError = p.stage === "error";
        const isRunning = !isDone && !isError;
        return (
          <div
            key={filename}
            className={cn(
              "rounded-md border px-2.5 py-1.5 text-[11px]",
              isError
                ? "border-red-500/40 bg-red-500/10 text-red-300"
                : isDone
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                  : "border-amber-500/40 bg-amber-500/10 text-amber-300",
            )}
          >
            <div className="flex items-center gap-1.5">
              {isRunning ? (
                <Loader2 className="w-3 h-3 animate-spin shrink-0" />
              ) : isDone ? (
                <Check className="w-3 h-3 shrink-0" />
              ) : (
                <span className="shrink-0">⚠</span>
              )}
              <span className="font-medium truncate flex-1" title={filename}>
                {filename}
              </span>
              <span className="opacity-80 shrink-0">{p.percent}%</span>
            </div>
            {/* Progress bar */}
            <div className="mt-1 h-1 rounded-full bg-black/20 overflow-hidden">
              <div
                className={cn(
                  "h-full transition-all duration-300",
                  isError
                    ? "bg-red-400"
                    : isDone
                      ? "bg-emerald-400"
                      : "bg-amber-400",
                )}
                style={{ width: `${p.percent}%` }}
              />
            </div>
            {/* Stage label */}
            <div className="mt-0.5 text-[10px] opacity-70 truncate">
              {isError ? p.error || p.details : p.details || p.stage}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── Thinking Block (per-segment, collapsible) ──────────────────────

function ThinkingBlockView({ block }: { block: MessageBlock }) {
  const [expanded, setExpanded] = useState<boolean>(false);

  // Currently thinking (streaming — no duration yet)
  if (block.duration == null) {
    return (
      <div className="mb-2">
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-2 py-2 text-foreground/60 font-medium mb-2 w-full text-left cursor-pointer"
          style={{ fontSize: "var(--app-font-size)" }}
        >
          <Loader2 className="w-5 h-5 animate-spin" />
          <span>Thinking...</span>
          {expanded ? (
            <ChevronDown className="w-3.5 h-3.5 shrink-0" />
          ) : (
            <ChevronRight className="w-3.5 h-3.5 shrink-0" />
          )}
        </button>
        {expanded && (
          <div className="ml-2.5 mt-1.5 border-l border-foreground/20 overflow-hidden animate-fade-in">
            <div className="px-3 text-[12px] text-foreground/70 leading-relaxed whitespace-pre-wrap">
              {block.content || "The assistant is formulating a response."}
            </div>
          </div>
        )}
      </div>
    );
  }

  // Thinking is done — show "Thought for Xs"
  return (
    <div className="mb-2">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 py-2 text-foreground/60 font-medium w-full text-left cursor-pointer"
        style={{ fontSize: "var(--app-font-size)" }}
      >
        <Brain className="w-5 h-5" />
        <span>Thought for {block.duration}s</span>
        {expanded ? (
          <ChevronDown className="w-3.5 h-3.5 shrink-0" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 shrink-0" />
        )}
      </button>
      {expanded && (
        <div className="ml-2.5 mt-1.5 border-l border-foreground/20 overflow-hidden animate-fade-in">
          <div className="px-3 text-[12px] text-foreground/70 leading-relaxed whitespace-pre-wrap">
            {block.content}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Text Block ──────────────────────────────────────────────────────

function TextBlockView({
  block,
  isStreaming,
}: {
  block: MessageBlock;
  isStreaming: boolean;
}) {
  return (
    <div
      className={cn(
        "prose prose-sm dark:prose-invert max-w-none leading-relaxed",
        isStreaming && "streaming-cursor",
      )}
      style={{ fontSize: "var(--app-font-size)" }}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {block.content || (isStreaming ? "" : "...")}
      </ReactMarkdown>
    </div>
  );
}

// ─── Error Block ─────────────────────────────────────────────────────

function ErrorBlockView({ block }: { block: MessageBlock }) {
  return (
    <div className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2 mb-2">
      <div className="flex items-start gap-2">
        <AlertCircle className="w-4 h-4 text-red-400 shrink-0 mt-0.5" />
        <p className="text-[12px] text-red-400 leading-relaxed">
          {block.content}
        </p>
      </div>
    </div>
  );
}

// ─── Tool Call Block (inline, expandable with max-height + scroll) ───

function StatusDot({ status }: { status: string }) {
  if (status === "running")
    return <span className="w-1.5 h-1.5 rounded-full bg-amber-400 pulse-dot" />;
  if (status === "completed")
    return <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />;
  if (status === "error")
    return <span className="w-1.5 h-1.5 rounded-full bg-red-400" />;
  return null;
}

function ToolCallBlockView({
  block,
  canViewPptx,
  onViewPptx,
}: {
  block: MessageBlock;
  isStreaming: boolean;
  canViewPptx: boolean;
  onViewPptx: (
    reportId: string,
    filename: string,
    downloadUrl: string,
    format: ViewerFormat,
  ) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const tc = block.toolCall;
  if (!tc) return null;

  // Detect deliverable-producing tool calls (report_gen / pptx_gen /
  // excel_gen all use ToolType.IMAGE_GEN but carry genResults with type
  // "report", "presentation" or "excel"). We override the label/icon
  // for these. Also detect during the "running" phase via the tool call
  // title.
  const hasDeliverable = tc.genResults?.some(
    (r) =>
      r.type === "report" || r.type === "presentation" || r.type === "excel",
  );
  const isPresentationDeliverable = tc.genResults?.some(
    (r) => r.type === "presentation",
  );
  const isExcelDeliverable = tc.genResults?.some((r) => r.type === "excel");
  // During "running" phase, genResults aren't set yet — detect via title.
  // The running event title is "Calling use_report_gen",
  // "Calling use_pptx_gen" or "Calling use_excel_gen".
  const titleLower = (tc.title || "").toLowerCase();
  const isReportTool =
    hasDeliverable ||
    (tc.status === "running" && titleLower.includes("report"));
  const isPresentationTool =
    isPresentationDeliverable ||
    (tc.status === "running" && titleLower.includes("pptx"));
  const isExcelTool =
    isExcelDeliverable ||
    (tc.status === "running" && titleLower.includes("excel"));

  const configs: Record<
    string,
    { icon: typeof Search; label: string; runningLabel: string; text: string }
  > = {
    websearch: {
      icon: Globe,
      label: "Searched the web",
      runningLabel: "Searching the web...",
      text: "text-blue-400",
    },
    vision: {
      icon: Eye,
      label: "Analyzed image",
      runningLabel: "Analyzing image...",
      text: "text-violet-400",
    },
    deepsearch: {
      icon: Brain,
      label: "Deep research",
      runningLabel: "Researching...",
      text: "text-purple-400",
    },
    code_exec: {
      icon: Code2,
      label: "Ran code",
      runningLabel: "Running code...",
      text: "text-emerald-400",
    },
    file_read: {
      icon: FileCode,
      label: "Read file",
      runningLabel: "Reading file...",
      text: "text-amber-400",
    },
    file_write: {
      icon: FileCode,
      label: "Wrote file",
      runningLabel: "Writing file...",
      text: "text-amber-400",
    },
    image_gen: {
      icon: Image,
      label: "Generated image",
      runningLabel: "Generating image...",
      text: "text-emerald-400",
    },
    report_gen: {
      icon: FileText,
      label: "Generated report",
      runningLabel: "Generating report...",
      text: "text-amber-400",
    },
    presentation_gen: {
      icon: FileText,
      label: "Generated presentation",
      runningLabel: "Generating presentation...",
      text: "text-amber-400",
    },
    excel_gen: {
      icon: FileSpreadsheet,
      label: "Generated spreadsheet",
      runningLabel: "Generating spreadsheet...",
      text: "text-emerald-400",
    },
  };

  // For image_gen type with deliverable genResults or report/pptx/excel
  // title during running, override to show document icon/label instead
  // of image icon/label.
  let effectiveType: string = tc.type;
  if (
    tc.type === "image_gen" &&
    (isReportTool || isPresentationTool || isExcelTool)
  ) {
    effectiveType = isPresentationTool
      ? "presentation_gen"
      : isExcelTool
        ? "excel_gen"
        : "report_gen";
  }

  const {
    icon: Icon,
    label,
    runningLabel,
    text,
  } = configs[effectiveType] ?? {
    icon: Zap,
    label: tc.title,
    runningLabel: tc.title,
    text: "text-muted-foreground",
  };

  // Display label: "Generating..." while running, "Generated ..." when done
  const displayLabel = tc.status === "running" ? runningLabel : label;

  const resultCount = tc.webResults?.length || tc.genResults?.length;
  const duration =
    tc.completedAt && tc.startedAt
      ? ((tc.completedAt - tc.startedAt) / 1000).toFixed(1)
      : null;

  // Auto-expand while running so the user sees progress, AND auto-expand
  // when completed with deliverables so the download badge is visible.
  const autoExpand =
    tc.status === "running" ||
    ((isReportTool || isPresentationTool || isExcelTool) &&
      tc.status === "completed");
  const isExpanded = expanded || autoExpand;

  return (
    <div className="my-2 rounded-xl border border-sandbox-border bg-sandbox-bg overflow-hidden">
      {/* Badge header — click to toggle */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          "w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-secondary/50 transition-colors",
        )}
      >
        {tc.status === "running" ? (
          <span className="w-3.5 h-3.5 border-[1.5px] border-current border-t-transparent rounded-full animate-spin shrink-0" />
        ) : (
          <Icon className={cn("w-3.5 h-3.5 shrink-0", text)} />
        )}
        <span className={cn("text-[12px] font-medium", text)}>
          {displayLabel}
        </span>
        {tc.query && (
          <span className="text-[11px] text-muted-foreground truncate">
            &quot;{tc.query}&quot;
          </span>
        )}
        {resultCount ? (
          <span className="text-[11px] opacity-60">· {resultCount}</span>
        ) : null}
        {duration ? (
          <span className="text-[11px] opacity-60">· {duration}s</span>
        ) : null}
        <StatusDot status={tc.status} />
        <div className="flex-1" />
        {isExpanded ? (
          <ChevronDown className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
        )}
      </button>

      {/* Expandable detail — max height + scroll for big outputs */}
      {isExpanded && (
        <div className="px-3 pb-3 max-h-96 overflow-y-auto border-t border-sandbox-border/50">
          <div className="pt-3 space-y-2">
            <ToolCallDetail
              tc={tc}
              canViewPptx={canViewPptx}
              onViewPptx={onViewPptx}
            />
            {/* RAG sources inside the tool call block */}
            {tc.sources && tc.sources.length > 0 && (
              <SourceCards sources={tc.sources} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function ToolCallDetail({
  tc,
  canViewPptx,
  onViewPptx,
}: {
  tc: ToolCallResult;
  canViewPptx: boolean;
  onViewPptx: (
    reportId: string,
    filename: string,
    downloadUrl: string,
    format: ViewerFormat,
  ) => void;
}) {
  if (tc.type === "websearch") return <WebSearchDetail tc={tc} />;
  if (tc.type === "vision") return <VisionDetail tc={tc} />;
  if (tc.type === "code_exec") return <CodeExecDetail tc={tc} />;
  if (tc.type === "image_gen") {
    // Check genResults for report/presentation/excel deliverables
    const hasDeliverable = tc.genResults?.some(
      (r) =>
        r.type === "report" || r.type === "presentation" || r.type === "excel",
    );
    // During "running" phase, genResults aren't set yet — detect via title
    const titleLower = (tc.title || "").toLowerCase();
    const isReportOrPptx =
      hasDeliverable ||
      (tc.status === "running" &&
        (titleLower.includes("report") ||
          titleLower.includes("pptx") ||
          titleLower.includes("excel")));
    if (isReportOrPptx) {
      return (
        <ReportGenDetail
          tc={tc}
          canViewPptx={canViewPptx}
          onViewPptx={onViewPptx}
        />
      );
    }
    return <ImageGenDetail tc={tc} />;
  }
  return <GenericToolDetail tc={tc} />;
}

function WebSearchDetail({ tc }: { tc: ToolCallResult }) {
  return (
    <div className="space-y-2">
      {tc.status === "running" && (
        <div className="flex items-center gap-2 px-2">
          <div className="w-3 h-3 border-[1.5px] border-blue-400 border-t-transparent rounded-full animate-spin" />
          <span className="text-[11px] text-muted-foreground">
            Searching the web...
          </span>
        </div>
      )}
      {tc.webResults && tc.webResults.length > 0 && (
        <div className="space-y-1.5">
          {tc.webResults.map((r, i) => (
            <div
              key={i}
              className="rounded-lg border border-border bg-card p-2.5 hover:bg-accent transition-colors cursor-pointer"
            >
              <div className="flex items-start gap-2.5">
                <div className="w-5 h-5 rounded bg-blue-500/10 flex items-center justify-center shrink-0 mt-0.5">
                  <span className="text-[9px] font-bold text-blue-400">
                    {i + 1}
                  </span>
                </div>
                <div className="min-w-0">
                  <p className="text-[12px] font-medium text-foreground truncate">
                    {r.title}
                  </p>
                  <p className="text-[11px] text-muted-foreground line-clamp-2 mt-0.5 leading-relaxed">
                    {r.snippet}
                  </p>
                  <p className="text-[10px] text-primary/50 truncate mt-1">
                    {r.url}
                  </p>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function VisionDetail({ tc }: { tc: ToolCallResult }) {
  return (
    <div className="space-y-2">
      {tc.status === "running" && (
        <div className="flex items-center gap-2 px-2">
          <div className="w-3 h-3 border-[1.5px] border-violet-400 border-t-transparent rounded-full animate-spin" />
          <span className="text-[11px] text-muted-foreground">
            Analyzing image with vision model...
          </span>
        </div>
      )}
      {tc.imageDescription && (
        <div className="rounded-lg border border-border overflow-hidden">
          <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border bg-card">
            <Eye className="w-3 h-3 text-violet-400" />
            <span className="text-[10px] text-muted-foreground">
              Vision Description
            </span>
          </div>
          <div className="p-3 text-[12px] text-foreground/80 leading-relaxed bg-sandbox-bg">
            {tc.imageDescription}
          </div>
        </div>
      )}
      {tc.error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2">
          <p className="text-[11px] text-red-400">{tc.error}</p>
        </div>
      )}
    </div>
  );
}

function CodeExecDetail({ tc }: { tc: ToolCallResult }) {
  return (
    <div className="space-y-2">
      {tc.status === "running" && (
        <div className="flex items-center gap-2 px-2">
          <div className="w-3 h-3 border-[1.5px] border-emerald-400 border-t-transparent rounded-full animate-spin" />
          <span className="text-[11px] text-muted-foreground">
            Executing code...
          </span>
        </div>
      )}
      {tc.code && (
        <div className="rounded-lg border border-border overflow-hidden">
          <div className="px-3 py-1.5 border-b border-border bg-card">
            <span className="text-[10px] text-muted-foreground font-mono">
              {tc.language ?? "code"}
            </span>
          </div>
          <pre className="p-3 text-[11px] text-emerald-300/70 font-mono overflow-x-auto leading-relaxed bg-sandbox-bg">
            <code>{tc.code}</code>
          </pre>
        </div>
      )}
      {tc.output && (
        <div className="rounded-lg border border-border overflow-hidden">
          <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border bg-card">
            <span className="text-[10px] text-muted-foreground">Output</span>
            {tc.exitCode !== undefined && (
              <span
                className={cn(
                  "text-[10px] px-1.5 py-0.5 rounded",
                  tc.exitCode === 0
                    ? "bg-emerald-500/10 text-emerald-400"
                    : "bg-red-500/10 text-red-400",
                )}
              >
                exit {tc.exitCode}
              </span>
            )}
          </div>
          <pre className="p-3 text-[11px] text-terminal-green font-mono overflow-x-auto leading-relaxed bg-sandbox-bg">
            {tc.output}
          </pre>
        </div>
      )}
    </div>
  );
}

function ImageGenDetail({ tc }: { tc: ToolCallResult }) {
  return (
    <div className="space-y-2">
      {tc.imageDescription && (
        <span className="text-[10px] text-muted-foreground">
          &quot;{tc.imageDescription}&quot;
        </span>
      )}
      {tc.genResults && tc.genResults.length > 0 && (
        <div className="space-y-2">
          {tc.genResults.map(
            (r, i) =>
              r.type === "image" && (
                <div
                  key={i}
                  className="rounded-lg border border-border overflow-hidden"
                >
                  <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border bg-card">
                    <span className="text-[10px] text-muted-foreground">
                      Result {i + 1}
                    </span>
                  </div>
                  <div className="p-3 text-center bg-sandbox-bg">
                    <img
                      src={`data:image/png;base64,${r.data}`}
                      alt={tc.imageDescription || "Generated image"}
                      className="mx-auto rounded max-h-64"
                    />
                  </div>
                </div>
              ),
          )}
        </div>
      )}
      {tc.error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2">
          <p className="text-[11px] text-red-400">{tc.error}</p>
        </div>
      )}
    </div>
  );
}

// ─── Report Deliverable Badge ────────────────────────────────────────

function ReportDeliverableBadge({
  filename,
  format,
  downloadUrl,
  thumbnailUrl,
  label,
  onView,
}: {
  filename: string;
  format: string;
  downloadUrl: string;
  thumbnailUrl?: string;
  label?: string;
  onView?: () => void;
}) {
  const [thumbError, setThumbError] = useState(false);
  const isPdf = format === "pdf";
  const isPptx = format === "pptx";
  const isXlsx = format === "xlsx";
  const iconBg = isPdf
    ? "bg-red-500/10"
    : isPptx
      ? "bg-amber-500/10"
      : isXlsx
        ? "bg-emerald-500/10"
        : "bg-blue-500/10";
  const iconColor = isPdf
    ? "text-red-400"
    : isPptx
      ? "text-amber-400"
      : isXlsx
        ? "text-emerald-400"
        : "text-blue-400";
  const labelColor = isPdf
    ? "text-red-400/70"
    : isPptx
      ? "text-amber-400/70"
      : isXlsx
        ? "text-emerald-400/70"
        : "text-blue-400/70";
  const displayLabel =
    label ?? (isPptx ? "Presentation" : isXlsx ? "Spreadsheet" : "Report");

  const showThumb = isPptx && thumbnailUrl && !thumbError;
  const canView = Boolean(onView);
  const viewTitle = isPptx
    ? "View presentation"
    : isXlsx
      ? "View spreadsheet"
      : "View document";

  return (
    <div className="flex items-center rounded-lg border border-border bg-card overflow-hidden hover:bg-accent/50 transition-colors group">
      {/* Clickable area: thumbnail/icon + filename → downloads */}
      <a
        href={downloadUrl}
        download={filename}
        className="flex items-center gap-3 flex-1 min-w-0 pl-2.5 pr-2 py-2 cursor-pointer"
        title={`Download ${filename}`}
      >
        {/* Thumbnail / Icon area */}
        {showThumb ? (
          <div className="w-20 h-14 shrink-0 relative bg-secondary/50 overflow-hidden rounded-md">
            <img
              src={thumbnailUrl}
              alt={filename}
              onError={() => setThumbError(true)}
              className="w-full h-full object-cover"
              loading="lazy"
            />
          </div>
        ) : (
          <div
            className={cn(
              "w-10 h-10 rounded-lg flex items-center justify-center shrink-0",
              iconBg,
            )}
          >
            <FileType className={cn("w-4 h-4", iconColor)} />
          </div>
        )}
        <div className="flex-1 min-w-0">
          <p className="text-[12px] font-medium text-foreground truncate">
            {filename}
          </p>
          <p className={cn("text-[10px] font-medium uppercase", labelColor)}>
            {format} {displayLabel}
          </p>
        </div>
      </a>

      {/* Action buttons — separate from download link */}
      <div className="flex items-center gap-0.5 pr-1.5 shrink-0">
        {canView && (
          <button
            onClick={onView}
            className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors"
            title={viewTitle}
          >
            <Eye className="w-4 h-4" />
          </button>
        )}
        <a
          href={downloadUrl}
          download={filename}
          className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          title="Download"
        >
          <Download className="w-4 h-4" />
        </a>
      </div>
    </div>
  );
}

function ReportGenDetail({
  tc,
  canViewPptx,
  onViewPptx,
}: {
  tc: ToolCallResult;
  canViewPptx: boolean;
  onViewPptx: (
    reportId: string,
    filename: string,
    downloadUrl: string,
    format: ViewerFormat,
  ) => void;
}) {
  const deliverableResults =
    tc.genResults?.filter(
      (r) =>
        r.type === "report" || r.type === "presentation" || r.type === "excel",
    ) ?? [];
  // During "running" phase, detect via title since genResults aren't set yet
  const titleLower = (tc.title || "").toLowerCase();
  const generatingLabel = titleLower.includes("pptx")
    ? "Generating presentation..."
    : titleLower.includes("excel")
      ? "Generating spreadsheet..."
      : "Generating report...";

  return (
    <div className="space-y-2">
      {tc.status === "running" && (
        <div className="flex items-center gap-2 px-2">
          <div className="w-3 h-3 border-[1.5px] border-emerald-400 border-t-transparent rounded-full animate-spin" />
          <span className="text-[11px] text-muted-foreground">
            {generatingLabel}
          </span>
        </div>
      )}
      {deliverableResults.length > 0 && (
        <div className="space-y-1.5">
          {deliverableResults.map((r, i) => {
            const fmt = (r.format as ViewerFormat) ?? "pptx";
            // PDFs are rasterized server-side without LibreOffice, so the
            // eye button is always offered; PPTX/DOCX/XLSX need LibreOffice
            // (XLSX workbooks are converted to paginated PDF by soffice —
            // the workbook's print setup controls the pagination).
            const viewable = !!r.report_id && (fmt === "pdf" || canViewPptx);
            return (
              <ReportDeliverableBadge
                key={i}
                filename={r.filename ?? "report"}
                format={r.format ?? "pdf"}
                downloadUrl={r.download_url ?? "#"}
                thumbnailUrl={r.thumbnail_url}
                label={
                  r.type === "presentation"
                    ? "Presentation"
                    : r.type === "excel"
                      ? "Spreadsheet"
                      : "Report"
                }
                onView={
                  viewable
                    ? () =>
                        onViewPptx(
                          r.report_id!,
                          r.filename ?? `report.${fmt}`,
                          r.download_url ?? "#",
                          fmt,
                        )
                    : undefined
                }
              />
            );
          })}
        </div>
      )}
      {tc.error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2">
          <p className="text-[11px] text-red-400">{tc.error}</p>
        </div>
      )}
    </div>
  );
}

function GenericToolDetail({ tc }: { tc: ToolCallResult }) {
  return (
    <div className="flex items-center gap-2">
      <Zap className="w-3.5 h-3.5 text-amber-400" />
      <span className="text-[12px] font-medium">{tc.title}</span>
      <StatusDot status={tc.status} />
    </div>
  );
}

// ─── Block Renderer ─────────────────────────────────────────────────

function BlockView({
  block,
  isStreaming,
  canViewPptx,
  onViewPptx,
}: {
  block: MessageBlock;
  isStreaming: boolean;
  canViewPptx: boolean;
  onViewPptx: (
    reportId: string,
    filename: string,
    downloadUrl: string,
    format: ViewerFormat,
  ) => void;
}) {
  switch (block.type) {
    case "thinking":
      return <ThinkingBlockView block={block} />;
    case "text":
      return <TextBlockView block={block} isStreaming={isStreaming} />;
    case "tool_call":
      return (
        <ToolCallBlockView
          block={block}
          isStreaming={isStreaming}
          canViewPptx={canViewPptx}
          onViewPptx={onViewPptx}
        />
      );
    case "error":
      return <ErrorBlockView block={block} />;
    default:
      return null;
  }
}

// ─── Response Time ────────────────────────────────────────────────

function formatResponseTime(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}m ${secs}s`;
}

// ─── Message Bubble ──────────────────────────────────────────────

export function MessageBubble({
  message,
  conversationId,
}: {
  message: Message;
  conversationId: string;
}) {
  const [copied, setCopied] = useState(false);
  const [isReading, setIsReading] = useState(false);
  const speechRef = useRef<SpeechSynthesisUtterance | null>(null);
  const isAssistant = message.role === "assistant";

  // PPTX viewer modal state
  const [libreOfficeAvailable, setLibreOfficeAvailable] = useState(false);
  const [viewingPptx, setViewingPptx] = useState<{
    reportId: string;
    filename: string;
    downloadUrl: string;
    format: ViewerFormat;
  } | null>(null);

  // Check if LibreOffice is installed (cached in depsClient)
  useEffect(() => {
    isLibreOfficeInstalled().then(setLibreOfficeAvailable);
  }, []);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback
      const textArea = document.createElement("textarea");
      textArea.value = message.content;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand("copy");
      document.body.removeChild(textArea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [message.content]);

  const handleReadAloud = useCallback(() => {
    if (!("speechSynthesis" in window)) return;

    if (isReading) {
      window.speechSynthesis.cancel();
      setIsReading(false);
      return;
    }

    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(message.content);
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    utterance.onend = () => setIsReading(false);
    utterance.onerror = () => setIsReading(false);
    speechRef.current = utterance;
    window.speechSynthesis.speak(utterance);
    setIsReading(true);
  }, [message.content, isReading]);

  const handleRegenerate = useCallback(() => {
    // Find the user message that precedes this assistant message
    const conv = useChatStore
      .getState()
      .conversations.find((c) => c.id === conversationId);
    if (!conv) return;
    const msgIndex = conv.messages.findIndex((m) => m.id === message.id);
    if (msgIndex < 1) return;

    const userMsg = conv.messages[msgIndex - 1];
    if (userMsg.role !== "user") return;

    // Remove this assistant message and re-trigger
    useChatStore.setState((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === conversationId
          ? { ...c, messages: c.messages.filter((m) => m.id !== message.id) }
          : c,
      ),
    }));

    // Find and trigger the InputArea's send — we'll dispatch a custom event
    window.dispatchEvent(
      new CustomEvent("regenerate-message", {
        detail: { content: userMsg.content, conversationId },
      }),
    );
  }, [conversationId, message.id]);

  // Calculate response time for assistant messages
  // Prefer DB-persisted generationDuration, fallback to frontend-computed time
  const responseTime = isAssistant
    ? message.generationDuration
      ? formatResponseTime(message.generationDuration * 1000)
      : message.completedAt && message.createdAt
        ? formatResponseTime(message.completedAt - message.createdAt)
        : null
    : null;

  // ─── User Message ─────────────────────────────────────────────
  if (!isAssistant) {
    return (
      <div className="flex justify-end animate-fade-in group">
        <div className="relative max-w-[85%] md:max-w-[75%]">
          {/* Shrug overlay */}
          {message.shrugOverlay && (
            <div className="text-center mb-1.5">
              <span
                className="text-[28px] leading-none font-medium select-none"
                style={{ color: "var(--color-primary)" }}
              >
                ¯\_(ツ)_/¯
              </span>
            </div>
          )}
          {/* Image/document indicators + voice-modality badge */}
          {(message.hasImage ||
            message.hasDocument ||
            message.modality === "voice") && (
            <div className="flex gap-1.5 mb-1.5 justify-end">
              {message.modality === "voice" && (
                <span
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-primary/10 text-[10px] text-primary"
                  title={t("voice.message.voiceSent")}
                >
                  <Mic className="w-2.5 h-2.5" />
                  {t("voice.message.voiceSent")}
                </span>
              )}
              {message.hasImage && (
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-blue-500/10 text-[10px] text-blue-400">
                  📷 {message.imageCount} image
                  {message.imageCount !== 1 ? "s" : ""}
                </span>
              )}
              {message.hasDocument && (
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-500/10 text-[10px] text-amber-400">
                  📄 {message.documentCount} doc
                  {message.documentCount !== 1 ? "s" : ""}
                </span>
              )}
            </div>
          )}
          {/* Inline document digestion progress — shown when the user
              uploaded docs via chat and the backend is streaming
              document_digest_* SSE events. Renders a compact progress
              bar per file so the user sees real-time feedback in the
              conversation itself, not just in the right-panel terminal. */}
          {message.digestProgress && message.digestProgress.length > 0 && (
            <DigestProgressIndicator items={message.digestProgress} />
          )}
          <div className="rounded-2xl bg-primary text-primary-foreground px-4 py-3">
            <p
              className="whitespace-pre-wrap leading-relaxed"
              style={{ fontSize: "var(--app-font-size)" }}
            >
              {message.content}
            </p>
          </div>
          {/* Copy icon */}
          <button
            onClick={handleCopy}
            className="absolute -bottom-6 right-0 opacity-0 group-hover:opacity-100 transition-opacity duration-150 p-1 rounded hover:bg-secondary cursor-pointer"
            title="Copy message"
          >
            {copied ? (
              <Check className="w-3.5 h-3.5 text-emerald-400" />
            ) : (
              <Copy className="w-3.5 h-3.5 text-muted-foreground" />
            )}
          </button>
        </div>
      </div>
    );
  }

  // ─── Assistant Message — render blocks in chronological order ──
  const blocks = message.blocks ?? [];
  const hasContent = blocks.length > 0 || message.content;

  return (
    <div className="animate-fade-in wrap-break-word">
      {blocks.map((block) => (
        <BlockView
          key={block.id}
          block={block}
          isStreaming={message.isStreaming}
          canViewPptx={libreOfficeAvailable}
          onViewPptx={(reportId, filename, downloadUrl, format) =>
            setViewingPptx({ reportId, filename, downloadUrl, format })
          }
        />
      ))}

      {/* Fallback: if no blocks but has content (e.g. loading from old data) */}
      {blocks.length === 0 && message.content && (
        <div
          className={cn(
            "prose prose-sm dark:prose-invert max-w-none leading-relaxed",
            message.isStreaming && "streaming-cursor",
          )}
          style={{ fontSize: "var(--app-font-size)" }}
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {message.content}
          </ReactMarkdown>
        </div>
      )}

      {/* Streaming placeholder when no blocks yet */}
      {blocks.length === 0 && !message.content && message.isStreaming && (
        <div className="flex items-center gap-2 text-muted-foreground py-2">
          <Loader2 className="w-4 h-4 animate-spin" />
          <span className="text-[12px]">Thinking...</span>
        </div>
      )}

      {/* Persisted deliverables (from DB) — rendered as download badges.
          These are also rendered inline inside tool_call blocks via
          genResults, but we show them here as a fallback when the
          blocks don't contain the full genResults (e.g. after refresh
          if the tool_call block's genResults weren't persisted). */}
      {message.deliverables && message.deliverables.length > 0 && (
        <div className="mt-3 space-y-1.5">
          {message.deliverables.map((d, i) => (
            <ReportDeliverableBadge
              key={i}
              filename={d.filename}
              format={d.format}
              downloadUrl={d.download_url}
              thumbnailUrl={d.thumbnail_url}
              onView={
                d.report_id &&
                ["pdf", "docx", "pptx", "xlsx"].includes(d.format) &&
                (d.format === "pdf" || libreOfficeAvailable)
                  ? () =>
                      setViewingPptx({
                        reportId: d.report_id!,
                        filename: d.filename,
                        downloadUrl: d.download_url,
                        format: (d.format as ViewerFormat) || "pptx",
                      })
                  : undefined
              }
            />
          ))}
        </div>
      )}

      {/* Action icons + response time */}
      {!message.isStreaming && hasContent && (
        <div className="flex items-center gap-1 mt-2.5">
          <button
            onClick={handleCopy}
            className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors"
            title="Copy"
          >
            {copied ? (
              <Check className="w-3.5 h-3.5 text-emerald-400" />
            ) : (
              <Copy className="w-3.5 h-3.5" />
            )}
          </button>
          <button
            onClick={handleReadAloud}
            className={cn(
              "p-1.5 rounded-md transition-colors",
              isReading
                ? "text-primary bg-primary/10"
                : "text-muted-foreground hover:text-foreground hover:bg-secondary",
            )}
            title={isReading ? "Stop reading" : "Read aloud"}
          >
            <Volume2 className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={handleRegenerate}
            className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors"
            title="Regenerate"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
          {message.modality === "voice" && (
            <span
              className="ml-1.5 inline-flex items-center select-none text-muted-foreground/50"
              title={t("voice.message.voiceSent")}
            >
              <Mic className="w-3 h-3" />
            </span>
          )}
          {responseTime && (
            <span className="text-[11px] text-muted-foreground/60 ml-1.5 select-none">
              {responseTime}
            </span>
          )}
        </div>
      )}

      {/* Document Viewer Modal (PPTX / PDF / DOCX) */}
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
