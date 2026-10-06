import { useState, useRef, useCallback, useEffect } from "react";
import {
  Brain,
  ChevronDown,
  ChevronRight,
  Eye,
  Copy,
  Volume2,
  RefreshCw,
  Check,
  Loader2,
  Image,
  FileCode,
  FileText,
  Quote,
  AlertCircle,
  Download,
  FileType,
  Mic,
  XCircle,
  Pause,
  Play,
  Square,
  RotateCcw,
  RotateCw,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { markdownCodeComponents } from "@/components/chat/MarkdownCodeBlock";
import { elapsedMilliseconds } from "@/lib/timing";
import { HighlightedCode } from "@/components/ui/HighlightedCode";
import type { Message, ToolCallResult, MessageBlock } from "@/store/chatStore";
import { formatWorkspaceTreeOutput } from "@/lib/workspaceTreeOutput";
import { useChatStore } from "@/store/chatStore";
import type { RetrievedSourceDTO } from "@/api/documentsClient";
import { isLibreOfficeInstalled } from "@/api/depsClient";
import { t, useT } from "@/store/settingsStore";
import {
  FileViewerModal,
  type ViewerFormat,
} from "@/components/chat/FileViewerModal";
import { cn } from "@/lib/utils";
import { formatCodeExecOutput } from "@/lib/codeExecOutput";
import { useUIStore } from "@/store/uiStore";
import { useSandboxStore } from "@/store/sandboxStore";
import { ReadAloudPlayer, type ReadingState } from "@/voice/readAloud";
import { DeckArtifact } from "@/components/learn/DeckArtifact";

// ─── Source Cards (RAG citations — rendered inside a tool_call block) ──

function SourceImage({ chunkId }: { chunkId: string }) {
  const translate = useT();
  // Lazy-load the image only when the card is expanded. We use a simple
  // <img> tag — the browser handles caching via the Cache-Control header
  // the backend sets.
  const [loaded, setLoaded] = useState(false);
  const [errored, setErrored] = useState(false);
  if (errored) return null;
  return (
    <div className="mt-1.5 rounded-md overflow-hidden border border-border/40 bg-background/40 max-w-70">
      {!loaded && (
        <div className="w-full h-30 flex items-center justify-center text-[11px] text-muted-foreground/80">
          {translate("tool.detail.loadingImage")}
        </div>
      )}
      <img
        src={`/api/documents/chunks/${chunkId}/image`}
        alt={translate("tool.detail.documentImage")}
        className={cn("w-full h-auto", loaded ? "block" : "hidden")}
        onLoad={() => setLoaded(true)}
        onError={() => setErrored(true)}
        loading="lazy"
      />
    </div>
  );
}

function SourceCards({ sources }: { sources: RetrievedSourceDTO[] }) {
  const translate = useT();
  const [expanded, setExpanded] = useState(true);
  const [openIdx, setOpenIdx] = useState<number | null>(0);

  if (!sources.length) return null;

  return (
    <div className="mt-1">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-1.5 py-1 text-left text-muted-foreground hover:text-foreground transition-colors rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
      >
        {expanded ? (
          <ChevronDown className="w-3 h-3 shrink-0" />
        ) : (
          <ChevronRight className="w-3 h-3 shrink-0" />
        )}
        <Quote className="w-3 h-3 shrink-0" />
        <span className="text-[12px] font-medium">
          {translate(
            sources.length === 1 ? "tool.detail.source" : "tool.detail.sources",
            { count: sources.length },
          )}
        </span>
        <span className="text-[11px] text-muted-foreground/80 truncate">
          {translate("tool.detail.retrievedDocuments")}
        </span>
      </button>

      {/* Source rows */}
      {expanded && (
        <div className="mt-1 ml-4.5 rounded-lg bg-background/60 divide-y divide-border/40 overflow-hidden">
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
              locationParts.length > 0 ? locationParts.join(", ") : "";
            const isImage = s.chunk_type === "image_description" && s.has_image;
            return (
              <div key={s.chunk_id}>
                {/* Row header */}
                <button
                  onClick={() => setOpenIdx(isOpen ? null : i)}
                  className="w-full flex items-center gap-2 px-2.5 py-1.5 text-left hover:bg-surface-hover/60 transition-colors"
                >
                  {isOpen ? (
                    <ChevronDown className="w-3 h-3 text-muted-foreground shrink-0" />
                  ) : (
                    <ChevronRight className="w-3 h-3 text-muted-foreground shrink-0" />
                  )}
                  <FileText className="w-3 h-3 text-muted-foreground/70 shrink-0" />
                  <span
                    className="text-[11.5px] font-medium text-foreground truncate flex-1"
                    title={s.document_filename}
                  >
                    {s.document_filename}
                  </span>
                  {isImage && (
                    <Image className="w-3 h-3 text-muted-foreground/80 shrink-0" />
                  )}
                  {location && (
                    <span className="text-[11px] text-muted-foreground/70 shrink-0">
                      {location}
                    </span>
                  )}
                  <span className="text-[11px] text-muted-foreground/70 shrink-0 tabular-nums">
                    {(s.score * 100).toFixed(0)}%
                  </span>
                </button>
                {/* Row body (snippet / full text + optional image) */}
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
              "rounded-lg px-2.5 py-1.5 text-[11px]",
              isError
                ? "bg-danger/10 text-danger"
                : isDone
                  ? "bg-success/10 text-success"
                  : "bg-warning/10 text-warning",
            )}
          >
            <div className="flex items-center gap-1.5">
              {isRunning ? (
                <Loader2 className="w-3 h-3 animate-spin shrink-0" />
              ) : isDone ? (
                <Check className="w-3 h-3 shrink-0" />
              ) : (
                <AlertCircle className="w-3 h-3 shrink-0" />
              )}
              <span className="font-medium truncate flex-1" title={filename}>
                {filename}
              </span>
              <span className="opacity-80 shrink-0 tabular-nums">
                {p.percent}%
              </span>
            </div>
            {/* Progress bar */}
            <div className="mt-1 h-1 rounded-full bg-foreground/10 overflow-hidden">
              <div
                className={cn(
                  "h-full transition-all duration-300",
                  isError ? "bg-danger" : isDone ? "bg-success" : "bg-warning",
                )}
                style={{ width: `${p.percent}%` }}
              />
            </div>
            {/* Stage label */}
            <div className="mt-0.5 text-[11px] opacity-70 truncate">
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
  const translate = useT();
  const [expanded, setExpanded] = useState<boolean>(false);
  const isThinking = block.duration == null;
  const roundedDuration = Math.max(0, Math.round(block.duration ?? 0));

  // Quiet disclosure — muted icon + muted label + chevron. Never competes
  // with the response below it; expanded content stays clearly secondary.
  return (
    <div className="mb-2">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 py-1 w-full text-left cursor-pointer rounded-md text-muted-foreground hover:text-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        style={{ fontSize: "var(--app-font-size)" }}
        aria-expanded={expanded}
      >
        {isThinking ? (
          <Loader2 className="w-5 h-5 animate-spin shrink-0" />
        ) : (
          <Brain className="w-5 h-5 shrink-0" />
        )}
        <span>
          {isThinking
            ? translate("message.thinking")
            : translate("message.thoughtFor", { seconds: roundedDuration })}
        </span>
        {expanded ? (
          <ChevronDown className="w-3 h-3 shrink-0" />
        ) : (
          <ChevronRight className="w-3 h-3 shrink-0" />
        )}
      </button>
      {expanded && (
        <div className="ml-1 mt-1 border-l-2 border-border/60 pl-3 animate-fade-in">
          <p className="text-[13px] text-muted-foreground leading-relaxed whitespace-pre-wrap">
            {block.content ||
              (isThinking ? translate("message.thinkingDescription") : "")}
          </p>
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
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={markdownCodeComponents}
      >
        {block.content || (isStreaming ? "" : "...")}
      </ReactMarkdown>
    </div>
  );
}

// ─── Error Block ─────────────────────────────────────────────────────

function ErrorBlockView({ block }: { block: MessageBlock }) {
  return (
    <div className="rounded-lg border border-danger/25 bg-danger/5 px-3 py-2 my-2">
      <div className="flex items-start gap-2">
        <AlertCircle className="w-4 h-4 text-danger shrink-0 mt-0.5" />
        <p className="text-[12.5px] text-danger leading-relaxed">
          {block.content}
        </p>
      </div>
    </div>
  );
}

// ─── Tool Call Block (compact activity row, expandable) ───────────

function StatusDot({ status }: { status: string }) {
  if (status === "running")
    return <span className="w-1.5 h-1.5 rounded-full bg-warning pulse-dot" />;
  if (status === "completed")
    return <span className="w-1.5 h-1.5 rounded-full bg-success" />;
  if (status === "error")
    return <span className="w-1.5 h-1.5 rounded-full bg-danger" />;
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
  const translate = useT();
  const [expanded, setExpanded] = useState(false);
  const tc = block.toolCall;
  if (!tc) return null;
  const deckArtifacts = tc.genResults?.filter(
    (r) => r.type === "flashcard_deck" && r.deck_id,
  );
  if (deckArtifacts?.length && tc.status === "completed")
    return (
      <>
        {deckArtifacts.map((deck) => (
          <DeckArtifact
            key={deck.deck_id}
            id={deck.deck_id!}
            title={deck.title ?? translate("learn.flashcards")}
            count={deck.card_count ?? 0}
          />
        ))}
      </>
    );

  // Detect deliverable-producing tool calls (report_gen / pptx_gen /
  // excel_gen all use ToolType.IMAGE_GEN but carry genResults with type
  // "report", "presentation" or "excel"). We override the label
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

  // Human-readable action labels per tool type — the collapsed activity
  // row shows ONLY this (plus status icon / duration / short target),
  // keeping the conversation quiet. Progressive disclosure: full detail
  // appears only when the row is expanded.
  const configs: Record<string, { label: string; runningLabel: string }> = {
    flashcards: {
      label: translate("learn.created"),
      runningLabel: translate("learn.creating"),
    },
    websearch: {
      label: translate("tool.websearch.done"),
      runningLabel: translate("tool.websearch.running"),
    },
    vision: {
      label: translate("tool.vision.done"),
      runningLabel: translate("tool.vision.running"),
    },
    deepsearch: {
      label: translate("tool.deepsearch.done"),
      runningLabel: translate("tool.deepsearch.running"),
    },
    code_exec: {
      label: translate("tool.code.done"),
      runningLabel: translate("tool.code.running"),
    },
    skill: {
      label: translate("tool.skill.done"),
      runningLabel: translate("tool.skill.running"),
    },
    file_read: {
      label: translate("tool.fileRead.done"),
      runningLabel: translate("tool.fileRead.running"),
    },
    file_write: {
      label: translate("tool.fileWrite.done"),
      runningLabel: translate("tool.fileWrite.running"),
    },
    image_gen: {
      label: translate("tool.image.done"),
      runningLabel: translate("tool.image.running"),
    },
    sandbox: {
      label: translate("tool.workspace.done"),
      runningLabel: translate("tool.workspace.running"),
    },
    preview: {
      label: translate("tool.preview.done"),
      runningLabel: translate("tool.preview.running"),
    },
    report_gen: {
      label: translate("tool.report.done"),
      runningLabel: translate("tool.report.running"),
    },
    presentation_gen: {
      label: translate("tool.presentation.done"),
      runningLabel: translate("tool.presentation.running"),
    },
    excel_gen: {
      label: translate("tool.spreadsheet.done"),
      runningLabel: translate("tool.spreadsheet.running"),
    },
  };

  // For image_gen type with deliverable genResults or report/pptx/excel
  // title during running, override to show document label instead
  // of image label.
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

  const { label, runningLabel } = configs[effectiveType] ?? {
    label: tc.title,
    runningLabel: tc.title,
  };

  // Display label: "Generating..." while running, "Generated ..." when done
  const displayLabel = tc.status === "running" ? runningLabel : label;

  // Short target shown inline after the label (file path for file tools,
  // query for searches) — keeps the row scannable.
  const shortTarget =
    tc.filePath && tc.filePath !== "/workspace"
      ? tc.filePath
      : tc.type === "websearch" || tc.type === "deepsearch"
        ? tc.query
        : undefined;

  const resultCount = tc.webResults?.length || tc.genResults?.length;
  const elapsed =
    Number.isFinite(tc.durationMs) && (tc.durationMs ?? -1) >= 0
      ? tc.durationMs!
      : elapsedMilliseconds(tc.startedAt, tc.completedAt);
  const duration = elapsed == null ? null : (elapsed / 1000).toFixed(1);

  // Auto-expand while running so the user sees progress, AND auto-expand
  // when completed with deliverables so the download badge is visible.
  const autoExpand =
    tc.status === "running" ||
    ((tc.type === "file_read" || tc.type === "file_write") &&
      tc.status === "completed") ||
    ((isReportTool || isPresentationTool || isExcelTool) &&
      tc.status === "completed");
  const isExpanded = expanded || autoExpand;

  return (
    <div className="my-1">
      {/* Activity row — click to toggle details */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="group flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-secondary/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        aria-expanded={isExpanded}
      >
        {/* Status icon — colored only for terminal states */}
        {tc.status === "running" ? (
          <Loader2 className="w-3.5 h-3.5 animate-spin shrink-0 text-muted-foreground" />
        ) : tc.status === "error" ? (
          <XCircle className="w-3.5 h-3.5 shrink-0 text-danger" />
        ) : (
          <Check className="w-3.5 h-3.5 shrink-0 text-success" />
        )}
        <span className="text-[12.5px] text-muted-foreground group-hover:text-foreground transition-colors truncate">
          {displayLabel}
        </span>
        {shortTarget && (
          <span className="text-[11.5px] text-muted-foreground/80 truncate">
            {shortTarget}
          </span>
        )}
        {resultCount ? (
          <span className="text-[11px] text-muted-foreground/70 shrink-0">
            · {resultCount}
          </span>
        ) : null}
        <span className="flex-1" />
        {duration ? (
          <span className="text-[11px] text-muted-foreground/80 tabular-nums shrink-0">
            {duration}s
          </span>
        ) : null}
        {isExpanded ? (
          <ChevronDown className="w-3 h-3 text-muted-foreground/80 shrink-0" />
        ) : (
          <ChevronRight className="w-3 h-3 text-muted-foreground/80 shrink-0" />
        )}
      </button>

      {/* Expandable detail — subtle secondary surface, max height + scroll
          for big outputs. Details only when expanded. */}
      {isExpanded && (
        <div className="ml-5 mt-0.5 mb-1 rounded-lg bg-secondary px-3 py-2.5 max-h-96 overflow-y-auto">
          <ToolCallDetail
            tc={tc}
            canViewPptx={canViewPptx}
            onViewPptx={onViewPptx}
          />
          {/* Live task-progress lines (deep research steps) */}
          {tc.steps && tc.steps.length > 0 && (
            <ul className="mt-2 space-y-1">
              {tc.steps.map((s, i) => (
                <li
                  key={i}
                  className="flex items-center gap-2 text-[12px] text-muted-foreground"
                >
                  {s.status === "done" ? (
                    <Check className="w-3 h-3 text-success shrink-0" />
                  ) : s.status === "running" ? (
                    <Loader2 className="w-3 h-3 animate-spin shrink-0" />
                  ) : (
                    <span
                      className="w-3 h-3 flex items-center justify-center shrink-0"
                      aria-hidden="true"
                    >
                      <span className="w-1 h-1 rounded-full bg-muted-foreground/40" />
                    </span>
                  )}
                  <span className="truncate">{s.label}</span>
                </li>
              ))}
            </ul>
          )}
          {/* RAG sources inside the tool call block */}
          {tc.sources && tc.sources.length > 0 && (
            <SourceCards sources={tc.sources} />
          )}
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
  if (tc.type === "file_read" || tc.type === "file_write")
    return <FileToolDetail tc={tc} />;
  if (tc.type === "preview") return <PreviewToolDetail tc={tc} />;
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

function FileToolDetail({ tc }: { tc: ToolCallResult }) {
  const translate = useT();
  const displayedContent = tc.fileContent
    ? formatWorkspaceTreeOutput(tc.fileContent)
    : "";
  const highlightContent =
    (tc.type === "file_read" || tc.type === "file_write") &&
    Boolean(tc.filePath) &&
    tc.filePath !== "/workspace";
  const openFile = async () => {
    if (!tc.filePath || tc.filePath === "/workspace") return;
    useUIStore.getState().setRightPanelOpen(true);
    useUIStore.getState().setRightPanelTab("code");
    await useSandboxStore.getState().fetchFileContent(tc.filePath);
  };
  const diffLines = tc.diff?.split("\n") ?? [];
  return (
    <div className="space-y-2">
      {tc.filePath && (
        <button
          type="button"
          onClick={() => void openFile()}
          disabled={tc.filePath === "/workspace"}
          className="flex items-center gap-1.5 font-mono text-[11.5px] text-primary hover:underline disabled:no-underline disabled:opacity-70"
          title={
            tc.filePath === "/workspace"
              ? translate("tool.detail.workspaceRoot")
              : translate("tool.detail.openFiles")
          }
        >
          <FileCode className="h-3.5 w-3.5" />
          {tc.filePath}
        </button>
      )}
      {tc.diff && (
        <div className="overflow-hidden rounded-lg border border-border/60">
          <div className="border-b border-border/40 bg-card/60 px-3 py-1 text-[11px] uppercase tracking-wider text-muted-foreground">
            {translate("tool.detail.diff")}
          </div>
          <pre className="overflow-x-auto bg-sandbox-bg p-3 font-mono text-[11.5px] leading-relaxed">
            {diffLines.map((line, index) => (
              <span
                key={`${index}-${line}`}
                className={cn(
                  "block min-h-lh whitespace-pre",
                  line.startsWith("---") ||
                    (line.startsWith("-") && !line.startsWith("---"))
                    ? "bg-danger/5 text-danger"
                    : line.startsWith("+++") || line.startsWith("+")
                      ? "bg-success/5 text-success"
                      : "text-muted-foreground",
                )}
              >
                {line}
              </span>
            ))}
          </pre>
        </div>
      )}
      {displayedContent && (
        <div className="overflow-hidden rounded-lg border border-border/60">
          <div className="border-b border-border/40 bg-card/60 px-3 py-1 text-[11px] uppercase tracking-wider text-muted-foreground">
            {tc.type === "file_write"
              ? translate("tool.detail.writtenContent")
              : translate("tool.detail.result")}
          </div>
          <pre className="max-h-64 overflow-auto bg-sandbox-bg p-3 font-mono text-[11.5px] leading-relaxed text-foreground/80">
            {highlightContent ? (
              <HighlightedCode code={displayedContent} filePath={tc.filePath} />
            ) : (
              displayedContent
            )}
          </pre>
        </div>
      )}
      {tc.error && <p className="text-[11.5px] text-danger">{tc.error}</p>}
    </div>
  );
}

function PreviewToolDetail({ tc }: { tc: ToolCallResult }) {
  const translate = useT();
  const openPreview = () => {
    if (tc.previewUrl)
      useSandboxStore.getState().selectPreview(tc.previewUrl, tc.previewPort);
    useUIStore.getState().setRightPanelOpen(true);
    useUIStore.getState().setRightPanelTab("preview");
  };
  return (
    <div className="space-y-2">
      {tc.code && <CodeExecDetail tc={tc} />}
      {tc.previewUrl && (
        <button
          type="button"
          onClick={openPreview}
          className="rounded-lg bg-primary/10 px-2.5 py-1.5 text-[11.5px] font-medium text-primary hover:bg-primary/20 transition-colors"
        >
          {translate("tool.detail.openPreview")}
          {tc.previewPort
            ? ` · ${translate("tool.detail.port", { port: tc.previewPort })}`
            : ""}
        </button>
      )}
    </div>
  );
}

function WebSearchDetail({ tc }: { tc: ToolCallResult }) {
  const translate = useT();
  return (
    <div className="space-y-2">
      {tc.status === "running" && (
        <div className="flex items-center gap-2">
          <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />
          <span className="text-[11.5px] text-muted-foreground">
            {translate("tool.websearch.running")}
          </span>
        </div>
      )}
      {tc.webResults && tc.webResults.length > 0 && (
        <div className="rounded-lg bg-background/60 divide-y divide-border/40 overflow-hidden">
          {tc.webResults.map((r, i) => (
            <div
              key={i}
              className="p-2.5 hover:bg-surface-hover/50 transition-colors cursor-pointer"
            >
              <div className="flex items-start gap-2.5">
                <span className="text-[11px] text-muted-foreground/80 tabular-nums shrink-0 mt-1 font-mono">
                  {i + 1}
                </span>
                <div className="min-w-0">
                  <p className="text-[12.5px] font-medium text-foreground truncate">
                    {r.title}
                  </p>
                  <p className="text-[11.5px] text-muted-foreground line-clamp-2 mt-0.5 leading-relaxed">
                    {r.snippet}
                  </p>
                  <p className="text-[10.5px] text-primary/60 truncate mt-1">
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
  const translate = useT();
  return (
    <div className="space-y-2">
      {tc.status === "running" && (
        <div className="flex items-center gap-2">
          <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />
          <span className="text-[11.5px] text-muted-foreground">
            {translate("tool.detail.analyzingVision")}
          </span>
        </div>
      )}
      {tc.imageDescription && (
        <div>
          <p className="text-[11px] uppercase tracking-wider text-muted-foreground/70 mb-1">
            {translate("tool.detail.visionDescription")}
          </p>
          <p className="text-[12.5px] text-foreground/85 leading-relaxed">
            {tc.imageDescription}
          </p>
        </div>
      )}
      {tc.error && <p className="text-[11.5px] text-danger">{tc.error}</p>}
    </div>
  );
}

function CodeExecDetail({ tc }: { tc: ToolCallResult }) {
  const translate = useT();
  const output = formatCodeExecOutput(tc.output);

  return (
    <div className="space-y-2">
      {tc.status === "running" && (
        <div className="flex items-center gap-2">
          <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />
          <span className="text-[11.5px] text-muted-foreground">
            {translate("tool.detail.executingCode")}
          </span>
        </div>
      )}
      {tc.code && (
        <div className="rounded-lg border border-border/60 overflow-hidden">
          <div className="px-3 py-1 border-b border-border/40 bg-card/60">
            <span className="text-[11px] text-muted-foreground font-mono uppercase tracking-wider">
              {tc.language ?? "code"}
            </span>
          </div>
          <pre className="overflow-x-auto bg-sandbox-bg p-3 font-mono text-[11.5px] leading-relaxed text-foreground/80">
            <HighlightedCode
              code={tc.code}
              language={tc.language ?? "python"}
            />
          </pre>
        </div>
      )}
      {output && (
        <div className="rounded-lg border border-border/60 overflow-hidden">
          <div className="flex items-center gap-2 px-3 py-1 border-b border-border/40 bg-card/60">
            <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
              {translate("tool.detail.output")}
            </span>
            {tc.exitCode !== undefined && (
              <span
                className={cn(
                  "text-[11px] px-1.5 py-0.5 rounded font-medium tabular-nums",
                  tc.exitCode === 0
                    ? "bg-success/10 text-success"
                    : "bg-danger/10 text-danger",
                )}
              >
                {translate("tool.detail.exitCode", { code: tc.exitCode })}
              </span>
            )}
          </div>
          <pre className="p-3 text-[12px] text-terminal-green font-mono overflow-x-auto leading-relaxed bg-sandbox-bg">
            {output}
          </pre>
        </div>
      )}
    </div>
  );
}

function ImageGenDetail({ tc }: { tc: ToolCallResult }) {
  const translate = useT();
  return (
    <div className="space-y-2">
      {tc.imageDescription && (
        <p className="text-[11.5px] text-muted-foreground/80 italic">
          &quot;{tc.imageDescription}&quot;
        </p>
      )}
      {tc.genResults && tc.genResults.length > 0 && (
        <div className="space-y-2">
          {tc.genResults.map(
            (r, i) =>
              r.type === "image" && (
                <div
                  key={i}
                  className="rounded-lg border border-border/60 overflow-hidden"
                >
                  <div className="px-3 py-1 border-b border-border/40 bg-card/60">
                    <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
                      {translate("tool.detail.numberedResult", {
                        number: i + 1,
                      })}
                    </span>
                  </div>
                  <div className="p-3 text-center bg-sandbox-bg">
                    <img
                      src={`data:image/png;base64,${r.data}`}
                      alt={tc.imageDescription || translate("tool.image.done")}
                      className="mx-auto rounded-md max-h-64"
                    />
                  </div>
                </div>
              ),
          )}
        </div>
      )}
      {tc.error && <p className="text-[11.5px] text-danger">{tc.error}</p>}
    </div>
  );
}

// ─── Report Deliverable Badge ────────────────────────────────────────

function ReportDeliverableBadge({
  filename,
  format,
  downloadUrl,
  label,
  onView,
}: {
  filename: string;
  format: string;
  downloadUrl: string;
  label?: string;
  onView?: () => void;
}) {
  const translate = useT();
  const isPptx = format === "pptx";
  const isXlsx = format === "xlsx";
  const displayLabel =
    label ??
    (isPptx
      ? translate("tool.detail.presentation")
      : isXlsx
        ? translate("tool.detail.spreadsheet")
        : translate("tool.detail.report"));
  const canView = Boolean(onView);
  const viewTitle = isPptx
    ? translate("tool.detail.viewPresentation")
    : isXlsx
      ? translate("tool.detail.viewSpreadsheet")
      : translate("tool.detail.viewDocument");

  return (
    <div className="flex items-center rounded-lg border border-border/60 bg-card px-2 transition-colors hover:bg-surface-hover/50 group">
      {/* Clickable area: icon + filename → downloads */}
      <a
        href={downloadUrl}
        download={filename}
        className="flex items-center gap-2.5 flex-1 min-w-0 pl-1.5 pr-1 py-1.5 cursor-pointer"
        title={translate("tool.detail.downloadFile", { filename })}
      >
        <span className="flex h-7 w-7 items-center justify-center rounded-md bg-secondary text-muted-foreground shrink-0">
          <FileType className="h-3.5 w-3.5" />
        </span>
        <span className="flex-1 min-w-0">
          <span className="block text-[12.5px] font-medium text-foreground truncate">
            {filename}
          </span>
          <span className="block text-[11px] uppercase tracking-wider text-muted-foreground">
            {format} · {displayLabel}
          </span>
        </span>
      </a>

      {/* Action buttons — separate from download link */}
      <div className="flex items-center gap-0.5 pr-1 shrink-0">
        {canView && (
          <button
            onClick={onView}
            aria-label={viewTitle}
            title={viewTitle}
            className="w-7 h-7 rounded-md flex items-center justify-center text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors"
          >
            <Eye className="w-3.5 h-3.5" />
          </button>
        )}
        <a
          href={downloadUrl}
          download={filename}
          aria-label={translate("tool.detail.download")}
          title={translate("tool.detail.download")}
          className="w-7 h-7 rounded-md flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-surface-hover transition-colors"
        >
          <Download className="w-3.5 h-3.5" />
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
  const translate = useT();
  const deliverableResults =
    tc.genResults?.filter(
      (r) =>
        r.type === "report" || r.type === "presentation" || r.type === "excel",
    ) ?? [];
  // During "running" phase, detect via title since genResults aren't set yet
  const titleLower = (tc.title || "").toLowerCase();
  const generatingLabel = titleLower.includes("pptx")
    ? translate("tool.presentation.running")
    : titleLower.includes("excel")
      ? translate("tool.spreadsheet.running")
      : translate("tool.report.running");

  return (
    <div className="space-y-2">
      {tc.status === "running" && (
        <div className="flex items-center gap-2">
          <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />
          <span className="text-[11.5px] text-muted-foreground">
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
                label={
                  r.type === "presentation"
                    ? translate("tool.detail.presentation")
                    : r.type === "excel"
                      ? translate("tool.detail.spreadsheet")
                      : translate("tool.detail.report")
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
      {tc.error && <p className="text-[11.5px] text-danger">{tc.error}</p>}
    </div>
  );
}

function GenericToolDetail({ tc }: { tc: ToolCallResult }) {
  return (
    <div className="flex items-center gap-2">
      <StatusDot status={tc.status} />
      <span className="text-[12.5px] text-muted-foreground">{tc.title}</span>
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
  const safeMs = Math.max(0, ms);
  if (safeMs < 1000) return `${Math.round(safeMs)}ms`;
  const seconds = safeMs / 1000;
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
  const [readingState, setReadingState] = useState<ReadingState>({
    loading: true,
    paused: false,
    canSeek: false,
    speed: 1,
  });
  const translate = useT();
  const speechRef = useRef<ReadAloudPlayer | null>(null);
  useEffect(() => () => speechRef.current?.stop(), []);
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
    if (isReading) {
      speechRef.current?.stop();
      setIsReading(false);
      return;
    }

    speechRef.current ??= new ReadAloudPlayer();
    setIsReading(true);
    void speechRef.current.play(
      message.content,
      () => setIsReading(false),
      setReadingState,
    );
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
    ? message.generationDuration != null
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
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-primary/10 text-[11px] font-medium text-primary"
                  title={t("voice.message.voiceSent")}
                >
                  <Mic className="w-2.5 h-2.5" />
                  {t("voice.message.voiceSent")}
                </span>
              )}
              {message.hasImage && (
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-secondary text-[11px] font-medium text-muted-foreground">
                  <Image className="w-2.5 h-2.5" />
                  {message.imageCount} image
                  {message.imageCount !== 1 ? "s" : ""}
                </span>
              )}
              {message.hasDocument && (
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-secondary text-[11px] font-medium text-muted-foreground">
                  <FileText className="w-2.5 h-2.5" />
                  {message.documentCount} doc
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
          <div className="rounded-xl bg-secondary px-4 py-2.5">
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
            className="absolute -bottom-6 right-0 opacity-0 group-hover:opacity-100 transition-opacity duration-150 p-1 rounded-md hover:bg-surface-hover cursor-pointer focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            aria-label="Copy message"
            title="Copy message"
          >
            {copied ? (
              <Check className="w-3.5 h-3.5 text-success" />
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
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={markdownCodeComponents}
          >
            {message.content}
          </ReactMarkdown>
        </div>
      )}

      {/* Streaming placeholder when no blocks yet */}
      {blocks.length === 0 && !message.content && message.isStreaming && (
        <div className="flex items-center gap-2 text-muted-foreground py-1.5">
          <Loader2 className="w-4 h-4 animate-spin" />
          <span className="text-[12.5px]">Thinking…</span>
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

      {/* Action icons + response time — quiet meta row under the content */}
      {!message.isStreaming && hasContent && (
        <div className="flex flex-wrap items-center gap-0.5 mt-2">
          <button
            onClick={handleCopy}
            className="p-1.5 rounded-md text-muted-foreground/70 hover:text-foreground hover:bg-surface-hover transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            aria-label="Copy"
            title="Copy"
          >
            {copied ? (
              <Check className="w-3.5 h-3.5 text-success" />
            ) : (
              <Copy className="w-3.5 h-3.5" />
            )}
          </button>
          <button
            onClick={handleReadAloud}
            className={cn(
              "p-1.5 rounded-md transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
              isReading
                ? "text-primary bg-primary/10"
                : "text-muted-foreground/70 hover:text-foreground hover:bg-surface-hover",
            )}
            title={translate(isReading ? "reading.stop" : "reading.start")}
            aria-label={translate(isReading ? "reading.stop" : "reading.start")}
          >
            <Volume2 className="w-3.5 h-3.5" />
          </button>
          {isReading && (
            <div
              role="group"
              aria-label={translate("reading.controls")}
              className="inline-flex h-6.5 items-center gap-0.5 text-muted-foreground/70"
            >
              {readingState.loading && (
                <Loader2
                  aria-label={translate("reading.loading")}
                  className="w-3.5 h-3.5 animate-spin text-muted-foreground"
                />
              )}
              <button
                className="inline-flex h-6.5 items-center gap-0.5 px-1.5 rounded-md hover:text-foreground hover:bg-surface-hover disabled:opacity-35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                disabled={readingState.loading || !readingState.canSeek}
                title={translate(
                  readingState.canSeek ? "reading.back" : "reading.noSeek",
                )}
                aria-label={translate("reading.back")}
                onClick={() => speechRef.current?.seek(-5)}
              >
                <RotateCcw className="w-3.5 h-3.5" />
                <span className="text-[9px]">5s</span>
              </button>
              <button
                className="inline-flex h-6.5 items-center p-1.5 rounded-md hover:text-foreground hover:bg-surface-hover disabled:opacity-35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                disabled={readingState.loading}
                title={translate(
                  readingState.paused ? "reading.resume" : "reading.pause",
                )}
                aria-label={translate(
                  readingState.paused ? "reading.resume" : "reading.pause",
                )}
                onClick={() => void speechRef.current?.togglePause()}
              >
                {readingState.paused ? (
                  <Play className="w-3.5 h-3.5" />
                ) : (
                  <Pause className="w-3.5 h-3.5" />
                )}
              </button>
              <button
                className="inline-flex h-6.5 items-center gap-0.5 px-1.5 rounded-md hover:text-foreground hover:bg-surface-hover disabled:opacity-35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                disabled={readingState.loading || !readingState.canSeek}
                title={translate(
                  readingState.canSeek ? "reading.forward" : "reading.noSeek",
                )}
                aria-label={translate("reading.forward")}
                onClick={() => speechRef.current?.seek(5)}
              >
                <RotateCw className="w-3.5 h-3.5" />
                <span className="text-[9px]">5s</span>
              </button>
              <button
                className="inline-flex h-6.5 items-center p-1.5 rounded-md hover:text-foreground hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                title={translate("reading.stop")}
                aria-label={translate("reading.stop")}
                onClick={() => speechRef.current?.stop()}
              >
                <Square className="w-3.5 h-3.5" />
              </button>
              <select
                className="h-6.5 bg-transparent text-[11px] rounded-md px-1 hover:text-foreground disabled:opacity-35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                aria-label={translate("reading.speed")}
                title={translate(
                  readingState.canSeek ? "reading.speed" : "reading.noSeek",
                )}
                disabled={readingState.loading || !readingState.canSeek}
                value={readingState.speed}
                onChange={(event) =>
                  speechRef.current?.setSpeed(Number(event.target.value))
                }
              >
                {readingState.speed === 0.5 && (
                  <option value={0.5}>×0.5</option>
                )}
                <option value={1}>×1</option>
                <option value={1.5}>×1.5</option>
                <option value={2}>×2</option>
              </select>
            </div>
          )}
          <button
            onClick={handleRegenerate}
            className="p-1.5 rounded-md text-muted-foreground/70 hover:text-foreground hover:bg-surface-hover transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            title="Regenerate"
            aria-label="Regenerate"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
          {message.modality === "voice" && (
            <span
              className="ml-1 inline-flex items-center select-none text-muted-foreground/70"
              title={t("voice.message.voiceSent")}
            >
              <Mic className="w-3 h-3" />
            </span>
          )}
          {responseTime && (
            <span className="text-[11px] text-muted-foreground/70 ml-1.5 select-none tabular-nums">
              {responseTime}
            </span>
          )}
        </div>
      )}

      {message.completionStatus === "interrupted" && (
        <div className="mt-2 inline-flex items-center gap-1.5 rounded-md bg-muted/60 px-2 py-1 text-[11px] font-medium text-muted-foreground">
          <XCircle className="h-3 w-3" />
          {t("message.interrupted")}
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
