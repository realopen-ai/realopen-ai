import { useState, useRef, useCallback } from "react";
import {
  Search,
  Code2,
  Zap,
  ChevronDown,
  Globe,
  FileCode,
  Brain,
  Copy,
  Volume2,
  RefreshCw,
  Check,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Message, ToolCallResult } from "@/store/chatStore";
import { useChatStore } from "@/store/chatStore";
import { cn } from "@/lib/utils";

// ─── Tool Badge ──────────────────────────────────────────────────

function ToolBadge({
  toolCall,
  onClick,
}: {
  toolCall: ToolCallResult;
  onClick: () => void;
}) {
  const configs: Record<
    string,
    { icon: typeof Search; label: string; bg: string; text: string }
  > = {
    websearch: {
      icon: Globe,
      label: "Searched the web",
      bg: "bg-blue-500/10",
      text: "text-blue-400",
    },
    deepsearch: {
      icon: Brain,
      label: "Deep research",
      bg: "bg-purple-500/10",
      text: "text-purple-400",
    },
    code_exec: {
      icon: Code2,
      label: "Ran code",
      bg: "bg-emerald-500/10",
      text: "text-emerald-400",
    },
    file_read: {
      icon: FileCode,
      label: "Read file",
      bg: "bg-amber-500/10",
      text: "text-amber-400",
    },
    file_write: {
      icon: FileCode,
      label: "Wrote file",
      bg: "bg-amber-500/10",
      text: "text-amber-400",
    },
  };
  const {
    icon: Icon,
    label,
    bg,
    text,
  } = configs[toolCall.type] ?? {
    icon: Zap,
    label: toolCall.title,
    bg: "bg-secondary",
    text: "text-muted-foreground",
  };
  const resultCount = toolCall.results?.length;
  const duration =
    toolCall.completedAt && toolCall.startedAt
      ? ((toolCall.completedAt - toolCall.startedAt) / 1000).toFixed(1)
      : null;

  return (
    <button
      onClick={onClick}
      className={cn(
        "tool-badge inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[12px] font-medium",
        bg,
        text,
      )}
    >
      {toolCall.status === "running" ? (
        <span className="w-3 h-3 border-[1.5px] border-current border-t-transparent rounded-full animate-spin" />
      ) : (
        <Icon className="w-3 h-3" />
      )}
      <span>{label}</span>
      {resultCount ? <span className="opacity-60">· {resultCount}</span> : null}
      {duration ? <span className="opacity-60">· {duration}s</span> : null}
    </button>
  );
}

// ─── Sandbox Panel (Per-Message) ─────────────────────────────────

function MessageSandbox({
  message,
  conversationId,
}: {
  message: Message;
  conversationId: string;
}) {
  const toggleSandbox = useChatStore((s) => s.toggleSandbox);
  if (!message.sandboxOpen || message.toolCalls.length === 0) return null;

  return (
    <div className="mt-2 rounded-xl border border-sandbox-border bg-sandbox-bg overflow-hidden animate-fade-in">
      <div className="flex items-center justify-between px-3.5 py-2 border-b border-sandbox-border">
        <div className="flex items-center gap-2">
          <div className="w-1.5 h-1.5 rounded-full bg-primary pulse-dot" />
          <span className="text-[12px] font-medium text-muted-foreground">
            Sandbox
          </span>
          <span className="text-[11px] text-muted-foreground/50">
            {message.toolCalls.length} tool{" "}
            {message.toolCalls.length === 1 ? "call" : "calls"}
          </span>
        </div>
        <button
          onClick={() => toggleSandbox(conversationId, message.id)}
          className="text-muted-foreground hover:text-foreground"
        >
          <ChevronDown className="w-3.5 h-3.5" />
        </button>
      </div>
      <div className="p-3.5 space-y-3 max-h-87.5 overflow-y-auto">
        {message.toolCalls.map((tc) => (
          <ToolCallDetail key={tc.id} toolCall={tc} />
        ))}
      </div>
    </div>
  );
}

function ToolCallDetail({ toolCall }: { toolCall: ToolCallResult }) {
  if (toolCall.type === "websearch") return <WebSearchDetail tc={toolCall} />;
  if (toolCall.type === "code_exec") return <CodeExecDetail tc={toolCall} />;
  if (toolCall.type === "deepsearch") return <DeepSearchDetail tc={toolCall} />;
  return <GenericToolDetail tc={toolCall} />;
}

function WebSearchDetail({ tc }: { tc: ToolCallResult }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Globe className="w-3.5 h-3.5 text-blue-400" />
        <span className="text-[12px] font-medium text-blue-400">
          Web Search
        </span>
        {tc.query && (
          <span className="text-[11px] text-muted-foreground">
            "{tc.query}"
          </span>
        )}
        <StatusDot status={tc.status} />
      </div>
      {tc.results && tc.results.length > 0 && (
        <div className="space-y-1.5">
          {tc.results.map((r, i) => (
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

function CodeExecDetail({ tc }: { tc: ToolCallResult }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Code2 className="w-3.5 h-3.5 text-emerald-400" />
        <span className="text-[12px] font-medium text-emerald-400">
          Code Execution
        </span>
        {tc.language && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400/70">
            {tc.language}
          </span>
        )}
        <StatusDot status={tc.status} />
      </div>
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

function DeepSearchDetail({ tc }: { tc: ToolCallResult }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Brain className="w-3.5 h-3.5 text-purple-400" />
        <span className="text-[12px] font-medium text-purple-400">
          Deep Research
        </span>
        <StatusDot status={tc.status} />
      </div>
      {tc.steps && tc.steps.length > 0 && (
        <div className="space-y-1.5 pl-1">
          {tc.steps.map((step, i) => (
            <div key={i} className="flex items-center gap-2.5">
              {step.status === "done" ? (
                <div className="w-4 h-4 rounded-full bg-purple-500/15 flex items-center justify-center">
                  <span className="text-[8px] text-purple-400">✓</span>
                </div>
              ) : step.status === "running" ? (
                <div className="w-4 h-4 rounded-full border-[1.5px] border-purple-400 border-t-transparent animate-spin" />
              ) : (
                <div className="w-4 h-4 rounded-full border border-muted-foreground/20" />
              )}
              <span
                className={cn(
                  "text-[12px]",
                  step.status === "done"
                    ? "text-foreground"
                    : step.status === "running"
                      ? "text-purple-400"
                      : "text-muted-foreground/60",
                )}
              >
                {step.label}
              </span>
            </div>
          ))}
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

function StatusDot({ status }: { status: string }) {
  if (status === "running")
    return <span className="w-1.5 h-1.5 rounded-full bg-amber-400 pulse-dot" />;
  if (status === "completed")
    return <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />;
  if (status === "error")
    return <span className="w-1.5 h-1.5 rounded-full bg-red-400" />;
  return null;
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
  const openSandbox = useChatStore((s) => s.openSandbox);
  const [copied, setCopied] = useState(false);
  const [isReading, setIsReading] = useState(false);
  const speechRef = useRef<SpeechSynthesisUtterance | null>(null);
  const isAssistant = message.role === "assistant";

  const handleBadgeClick = () => {
    openSandbox(conversationId, message.id);
  };

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

    // Cancel any ongoing speech
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
  const responseTime =
    isAssistant && message.completedAt && message.createdAt
      ? formatResponseTime(message.completedAt - message.createdAt)
      : null;

  // ─── User Message ─────────────────────────────────────────────
  if (!isAssistant) {
    return (
      <div className="flex justify-end animate-fade-in group">
        <div className="relative max-w-[85%] md:max-w-[75%]">
          <div className="rounded-2xl bg-primary text-primary-foreground px-4 py-3">
            <p className="text-[14px] whitespace-pre-wrap leading-relaxed">
              {message.content}
            </p>
          </div>
          {/* Copy icon — visible on hover */}
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

  // ─── Assistant Message ────────────────────────────────────────
  return (
    <div className="animate-fade-in">
      {/* Tool Call Badges */}
      {message.toolCalls.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2">
          {message.toolCalls.map((tc) => (
            <ToolBadge key={tc.id} toolCall={tc} onClick={handleBadgeClick} />
          ))}
        </div>
      )}

      {/* Content — full width, no background */}
      <div
        className={cn(
          "prose prose-sm dark:prose-invert max-w-none text-[14px] leading-relaxed",
          message.isStreaming && "streaming-cursor",
        )}
      >
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {message.content || (message.isStreaming ? "" : "...")}
        </ReactMarkdown>
      </div>

      {/* Per-Message Sandbox */}
      <MessageSandbox message={message} conversationId={conversationId} />

      {/* Action icons + response time — only show when streaming is complete */}
      {!message.isStreaming && message.content && (
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
          {responseTime && (
            <span className="text-[11px] text-muted-foreground/60 ml-1.5 select-none">
              {responseTime}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
