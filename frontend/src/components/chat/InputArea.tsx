import { useState, useRef, useEffect, useMemo } from "react";
import {
  Send,
  Loader2,
  Paperclip,
  Globe,
  ChevronDown,
  Slash,
  Sparkles,
  Brain,
  SmilePlus,
  X,
  Image as ImageIcon,
  FileText,
  Mic,
  MicOff,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from "@/components/ui/tooltip";
import { useChatStore } from "@/store/chatStore";
import { t } from "@/store/settingsStore";
import { useVoiceStore, type VoiceUiState } from "@/voice/voiceStore";
import { cn } from "@/lib/utils";

// ─── Slash Command Definitions ────────────────────────────────────

export interface SlashCommand {
  name: string;
  description: string;
  icon: React.ReactNode;
  /** If true, this command changes the model for this message only */
  modelOverride?: string;
  /** If true, this command adds the shrug overlay */
  shrug?: boolean;
}

export const slashCommands: SlashCommand[] = [
  {
    name: "shrug",
    description: t("input.slash.shrug"),
    icon: <SmilePlus className="w-4 h-4" />,
    shrug: true,
  },
  {
    name: "think",
    description: t("input.slash.think"),
    icon: <Brain className="w-4 h-4" />,
    modelOverride: "default_reasoning",
  },
  {
    name: "imagine",
    description: t("input.slash.imagine"),
    icon: <Sparkles className="w-4 h-4" />,
    modelOverride: "default_image_gen",
  },
];

// ─── Slash Command Menu ───────────────────────────────────────────

function SlashCommandMenu({
  commands,
  selectedIndex,
  onSelect,
}: {
  commands: SlashCommand[];
  selectedIndex: number;
  onSelect: (cmd: SlashCommand) => void;
}) {
  return (
    <div className="absolute bottom-full left-0 right-0 mb-2 max-w-3xl mx-auto z-40">
      <div className="bg-popover border border-border rounded-xl shadow-2xl overflow-hidden">
        <div className="px-3 py-2 border-b border-border/50">
          <div className="flex items-center gap-1.5 text-muted-foreground">
            <Slash className="w-3.5 h-3.5" />
            <span className="text-[12px] font-medium">{t("input.slash")}</span>
            <span className="ml-auto flex items-center gap-1 text-[10px] text-muted-foreground/50">
              <kbd className="inline-flex items-center justify-center px-1.5 py-0.5 rounded bg-secondary border border-border/50 text-[9px] font-mono leading-none">
                ↹
              </kbd>
              <span>autocomplete</span>
            </span>
          </div>
        </div>
        <div className="py-1 max-h-50 overflow-y-auto">
          {commands.length === 0 ? (
            <div className="px-3 py-4 text-center text-[12px] text-muted-foreground">
              {t("input.slash.noCommands")}
            </div>
          ) : (
            commands.map((cmd, i) => (
              <button
                key={cmd.name}
                onClick={() => onSelect(cmd)}
                className={cn(
                  "w-full flex items-center gap-3 px-3 py-2.5 text-left transition-colors",
                  i === selectedIndex
                    ? "bg-accent text-foreground"
                    : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
                )}
              >
                <div
                  className={cn(
                    "w-7 h-7 rounded-lg flex items-center justify-center shrink-0",
                    i === selectedIndex
                      ? "bg-primary/15 text-primary"
                      : "bg-secondary text-muted-foreground",
                  )}
                >
                  {cmd.icon}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium text-foreground">
                      /{cmd.name}
                    </span>
                  </div>
                  <p className="text-[11px] text-muted-foreground truncate">
                    {cmd.description}
                  </p>
                </div>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

// ─── File Attachment Preview ──────────────────────────────────────

interface AttachedFile {
  file: File;
  type: "image" | "document";
  previewUrl?: string;
}

function AttachmentPreview({
  attachment,
  onRemove,
}: {
  attachment: AttachedFile;
  onRemove: () => void;
}) {
  const isImage = attachment.type === "image";

  return (
    <div className="relative group flex items-center gap-2 px-2 py-1.5 rounded-lg bg-secondary/50 border border-border/50 max-w-40">
      {isImage && attachment.previewUrl ? (
        <img
          src={attachment.previewUrl}
          alt={attachment.file.name}
          className="w-8 h-8 rounded object-cover shrink-0"
        />
      ) : (
        <div className="w-8 h-8 rounded bg-secondary flex items-center justify-center shrink-0">
          {isImage ? (
            <ImageIcon className="w-4 h-4 text-blue-400" />
          ) : (
            <FileText className="w-4 h-4 text-amber-400" />
          )}
        </div>
      )}
      <div className="min-w-0 flex-1">
        <p className="text-[11px] text-foreground truncate">
          {attachment.file.name}
        </p>
        <p className="text-[10px] text-muted-foreground/60">
          {(attachment.file.size / 1024).toFixed(0)} KB
        </p>
      </div>
      <button
        onClick={onRemove}
        className="absolute -top-1.5 -right-1.5 w-4.5 h-4.5 rounded-full bg-destructive text-destructive-foreground flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
      >
        <X className="w-3 h-3" />
      </button>
    </div>
  );
}

// ─── Parsed command result ────────────────────────────────────────

export interface ParsedSlashCommand {
  command: SlashCommand | null;
  remainingContent: string;
}

export function parseSlashCommand(input: string): ParsedSlashCommand {
  const trimmed = input.trim();
  // Only parse if starts with /
  if (!trimmed.startsWith("/"))
    return { command: null, remainingContent: input };

  const parts = trimmed.split(/\s+/);
  const cmdName = parts[0].slice(1); // remove the /
  const remaining = parts.slice(1).join(" ");

  const cmd = slashCommands.find((c) => c.name === cmdName);
  if (!cmd) return { command: null, remainingContent: input };

  return {
    command: cmd,
    remainingContent: remaining || input.replace(parts[0], "").trim(),
  };
}

// ─── Voice: mic button + status strip ──────────────────────────────

/** Animated equalizer bars — shown while the assistant is speaking. */
function VoiceEqBars({ className }: { className?: string }) {
  return (
    <span className={cn("voice-eq-bars", className)} aria-hidden="true">
      <span />
      <span />
      <span />
      <span />
    </span>
  );
}

/** Map a voice error code to an actionable, localized message. */
function voiceErrorMessage(code: string, message: string): string {
  switch (code) {
    case "mic_denied":
      return t("voice.error.micDenied");
    case "mic_unavailable":
      return t("voice.error.micUnavailable");
    case "not_ready":
      return t("voice.error.notReady");
    case "runtime_missing":
      return t("voice.error.runtimeMissing", { name: message });
    case "connection_closed":
    case "connection_failed":
      return t("voice.error.connection");
    default:
      return t("voice.error.default", { message });
  }
}

/** Mic button — one visual per voice state, with per-state tooltip and
 *  aria-label. Clicking while the assistant speaks = interrupt (barge-in).
 *  The button is never disabled by streaming — voice stays usable while
 *  text chat is streaming (mixed modality). */
function VoiceMicButton({
  voiceState,
  disabled,
  disabledReason,
  onToggle,
}: {
  voiceState: VoiceUiState;
  disabled: boolean;
  disabledReason: string | null;
  onToggle: () => void;
}) {
  const isActive = voiceState !== "inactive" && voiceState !== "error";

  const stateTooltipKey = (() => {
    switch (voiceState) {
      case "connecting":
        return "voice.button.connecting";
      case "listening":
        return "voice.button.listening";
      case "processing":
        return "voice.button.processing";
      case "speaking":
        return "voice.button.speaking";
      case "interrupting":
        return "voice.button.interrupting";
      case "stopping":
        return "voice.button.stopping";
      case "error":
        return "voice.button.error";
      default:
        return "voice.button.start";
    }
  })();

  const tooltip = disabled
    ? (disabledReason ?? t("voice.button.setupRequired"))
    : t(stateTooltipKey);

  const buttonBody = (() => {
    switch (voiceState) {
      case "connecting":
      case "stopping":
      case "processing":
        return <Loader2 className="w-4.5 h-4.5 animate-spin" />;
      case "speaking":
        return <VoiceEqBars className="h-4.5 w-4.5" />;
      case "listening":
        return (
          <span className="relative flex items-center justify-center w-4.5 h-4.5">
            {/* pulsing ring — listening indicator */}
            <span className="absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-60 animate-ping" />
            <Mic className="relative w-4 h-4" />
          </span>
        );
      case "interrupting":
        return <MicOff className="w-4.5 h-4.5" />;
      default:
        return <Mic className="w-4.5 h-4.5" />;
    }
  })();

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          aria-label={tooltip}
          aria-pressed={isActive}
          onClick={onToggle}
          disabled={disabled}
          className={cn(
            "h-8 w-8 shrink-0 transition-all",
            // Default / inactive
            voiceState === "inactive" &&
              "text-muted-foreground/50 hover:text-muted-foreground",
            // Listening — emerald, active
            voiceState === "listening" &&
              "text-emerald-500 hover:text-emerald-400",
            // Processing / connecting / stopping — primary
            (voiceState === "connecting" ||
              voiceState === "processing" ||
              voiceState === "stopping") &&
              "text-primary hover:text-primary",
            // Speaking — animated bars, primary
            voiceState === "speaking" && "text-primary hover:text-primary/80",
            // Interrupting / error — red flash
            (voiceState === "interrupting" || voiceState === "error") &&
              "text-red-500 hover:text-red-400 voice-error-flash",
            // Disabled — visually muted
            disabled && "opacity-50 cursor-not-allowed",
          )}
        >
          {buttonBody}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top">{tooltip}</TooltipContent>
    </Tooltip>
  );
}

/** Voice status strip — state label + live partial transcript + inline
 *  error alert. Rendered above the input while voice is active; the error
 *  alert also renders while INACTIVE when a voiceError is set (e.g. mic
 *  permission denied during connect — the session tears down to inactive
 *  but the actionable message must stay visible until dismissed). Never
 *  blocks the normal UI (textarea + send remain fully usable). */
function VoiceStatusStrip({ onInterrupt }: { onInterrupt: () => void }) {
  const voiceState = useVoiceStore((s) => s.voiceState);
  const partialTranscript = useVoiceStore((s) => s.partialTranscript);
  const voiceError = useVoiceStore((s) => s.voiceError);
  const clearVoiceError = useVoiceStore((s) => s.clearVoiceError);

  if (voiceState === "inactive" && !voiceError) return null;
  const showStateRow = voiceState !== "inactive";

  const stateLabelKey = (() => {
    switch (voiceState) {
      case "connecting":
        return "voice.state.connecting";
      case "listening":
        return "voice.state.listening";
      case "processing":
        return "voice.state.processing";
      case "speaking":
        return "voice.state.speaking";
      case "interrupting":
        return "voice.state.interrupted";
      case "stopping":
        return "voice.state.stopping";
      case "error":
        return "voice.state.error";
      default:
        return "voice.state.inactive";
    }
  })();

  const stateColor = (() => {
    switch (voiceState) {
      case "listening":
        return "text-emerald-500";
      case "processing":
      case "connecting":
      case "speaking":
        return "text-primary";
      case "interrupting":
      case "error":
        return "text-red-500";
      default:
        return "text-muted-foreground";
    }
  })();

  const errorMessage = voiceError
    ? voiceErrorMessage(voiceError.code, voiceError.message)
    : null;

  return (
    <div className="mb-1.5 px-1 space-y-1">
      {/* State + partial transcript (only while a session is active) */}
      {showStateRow && (
        <div className="flex items-center gap-2 min-w-0" role="status">
          {voiceState === "speaking" ? (
            <VoiceEqBars className="h-3 w-3 text-primary" />
          ) : (
            <span
              className={cn(
                "w-2 h-2 rounded-full shrink-0",
                voiceState === "listening" && "bg-emerald-500 animate-pulse",
                voiceState === "processing" && "bg-primary animate-pulse",
                voiceState === "connecting" && "bg-primary/60 animate-pulse",
                voiceState === "stopping" && "bg-muted-foreground/50",
                voiceState === "interrupting" && "bg-red-500",
                voiceState === "error" && "bg-red-500",
              )}
            />
          )}
          <span className={cn("text-[11px] font-medium shrink-0", stateColor)}>
            {t(stateLabelKey)}
          </span>
          {/* Live partial transcript (subtle, typewriter-ish) */}
          {(voiceState === "listening" || voiceState === "processing") && (
            <span
              className="text-[11px] text-muted-foreground/70 truncate"
              aria-live="polite"
            >
              {partialTranscript
                ? `“${partialTranscript}”`
                : t("voice.partial.placeholder")}
            </span>
          )}
          {/* Interrupt affordance while the assistant speaks */}
          {voiceState === "speaking" && (
            <button
              onClick={onInterrupt}
              className="text-[11px] text-muted-foreground hover:text-foreground underline underline-offset-2 shrink-0 transition-colors"
            >
              {t("voice.interrupt")}
            </button>
          )}
        </div>
      )}

      {/* Inline error alert with actionable text */}
      {errorMessage && (
        <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/5 px-2.5 py-1.5 animate-fade-in">
          <MicOff className="w-3.5 h-3.5 text-red-500 shrink-0 mt-0.5" />
          <p className="text-[11px] text-red-500 leading-relaxed flex-1">
            {errorMessage}
          </p>
          <button
            onClick={clearVoiceError}
            aria-label={t("voice.error.dismiss")}
            className="text-red-500/60 hover:text-red-500 shrink-0 p-0.5"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Input Area ───────────────────────────────────────────────────

export function InputArea({
  onSend,
  isStreaming,
  onToggleVoice,
  onInterruptSpeaking,
}: {
  onSend: (
    message: string,
    options?: {
      modelOverride?: string;
      shrug?: boolean;
      images?: File[];
      documents?: File[];
    },
  ) => void;
  isStreaming: boolean;
  /** Toggle voice mode (connect+start / stop; interrupt while speaking). */
  onToggleVoice: () => void;
  /** Explicit barge-in affordance while the assistant speaks. */
  onInterruptSpeaking: () => void;
}) {
  const [input, setInput] = useState("");
  const [showSlashMenu, setShowSlashMenu] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [attachments, setAttachments] = useState<AttachedFile[]>([]);

  // ── Voice state (global store — one session per ChatArea) ──
  const voiceState = useVoiceStore((s) => s.voiceState);
  const readiness = useVoiceStore((s) => s.readiness);
  const voiceSetupDisabled = readiness !== null && !readiness.ready;
  const missingItems = readiness?.missing ?? [];
  const voiceDisabledReason = voiceSetupDisabled
    ? missingItems.length > 0
      ? `${t("voice.button.setupRequired")} — ${missingItems.join(", ")}`
      : t("voice.button.setupRequired")
    : null;
  const models = useChatStore((s) => s.models);
  const selectedModel = useChatStore((s) => s.selectedModel);
  const setSelectedModel = useChatStore((s) => s.setSelectedModel);
  const [showModelMenu, setShowModelMenu] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const docInputRef = useRef<HTMLInputElement>(null);

  // ── Slash menu filtering ──
  const slashFilter = useMemo(() => {
    const trimmed = input.trimStart();
    if (!trimmed.startsWith("/")) return "";
    const firstWord = trimmed.split(/\s/)[0].slice(1);
    if (trimmed.includes(" ")) return "";
    return firstWord.toLowerCase();
  }, [input]);

  const filteredCommands = useMemo(() => {
    if (!slashFilter && !showSlashMenu) return [];
    if (!slashFilter) return slashCommands;
    return slashCommands.filter((cmd) =>
      cmd.name.toLowerCase().startsWith(slashFilter),
    );
  }, [slashFilter, showSlashMenu]);

  // Show menu when typing / at start
  useEffect(() => {
    const trimmed = input.trimStart();
    if (trimmed.startsWith("/") && !trimmed.includes(" ")) {
      setShowSlashMenu(true);
      setSelectedIndex(0);
    } else {
      setShowSlashMenu(false);
    }
  }, [input]);

  useEffect(() => {
    const ta = textareaRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
    }
  }, [input]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node))
        setShowModelMenu(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const handleSlashSelect = (cmd: SlashCommand) => {
    const trimmed = input.trimStart();
    const parts = trimmed.split(/\s+/);
    const afterCommand = parts.slice(1).join(" ");

    const newInput = `/${cmd.name} ${afterCommand.trim()}`;
    setInput(newInput);
    setShowSlashMenu(false);

    setTimeout(() => textareaRef.current?.focus(), 0);
  };

  // ── File attachment handling ──

  const handleImageSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;

    const newAttachments: AttachedFile[] = [];
    for (const file of Array.from(files)) {
      if (file.type.startsWith("image/")) {
        const previewUrl = URL.createObjectURL(file);
        newAttachments.push({ file, type: "image", previewUrl });
      }
    }
    setAttachments((prev) => [...prev, ...newAttachments]);
    // Reset the input so the same file can be selected again
    e.target.value = "";
  };

  const handleDocumentSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;

    const newAttachments: AttachedFile[] = [];
    for (const file of Array.from(files)) {
      newAttachments.push({ file, type: "document" });
    }
    setAttachments((prev) => [...prev, ...newAttachments]);
    e.target.value = "";
  };

  const removeAttachment = (index: number) => {
    setAttachments((prev) => {
      const removed = prev[index];
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
      return prev.filter((_, i) => i !== index);
    });
  };

  const handleSend = () => {
    const trimmed = input.trim();
    if ((!trimmed && attachments.length === 0) || isStreaming) return;

    const { command, remainingContent } = parseSlashCommand(trimmed);
    const contentToSend = command
      ? remainingContent || trimmed.replace(/^\/\S+\s*/, "").trim() || trimmed
      : trimmed;

    const finalContent = command?.shrug
      ? remainingContent || "¯\\_(ツ)_/¯"
      : contentToSend;

    // Separate attachments into images and documents
    const images = attachments
      .filter((a) => a.type === "image")
      .map((a) => a.file);
    const documents = attachments
      .filter((a) => a.type === "document")
      .map((a) => a.file);

    onSend(finalContent || trimmed, {
      modelOverride: command?.modelOverride,
      shrug: command?.shrug,
      images: images.length > 0 ? images : undefined,
      documents: documents.length > 0 ? documents : undefined,
    });

    // Cleanup preview URLs
    for (const a of attachments) {
      if (a.previewUrl) URL.revokeObjectURL(a.previewUrl);
    }

    setInput("");
    setAttachments([]);
    setShowSlashMenu(false);
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Slash menu navigation
    if (showSlashMenu && filteredCommands.length > 0) {
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((prev) =>
          prev <= 0 ? filteredCommands.length - 1 : prev - 1,
        );
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((prev) =>
          prev >= filteredCommands.length - 1 ? 0 : prev + 1,
        );
        return;
      }
      if (e.key === "Tab" || e.key === "Enter") {
        if (filteredCommands[selectedIndex]) {
          e.preventDefault();
          handleSlashSelect(filteredCommands[selectedIndex]);
          return;
        }
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setShowSlashMenu(false);
        return;
      }
    }

    // Normal send
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const getSelectedModelLabel = () => {
    if (selectedModel === "default") {
      const m = models.find((m) => m.role === "default");
      return m ? m.description : "Default";
    }
    const m = models.find(
      (m) => m.id === selectedModel || m.role === selectedModel,
    );
    return m ? m.description : selectedModel;
  };

  const modelGroups = models.reduce<Record<string, typeof models>>((acc, m) => {
    const type = m.type || "other";
    if (!acc[type]) acc[type] = [];
    acc[type].push(m);
    return acc;
  }, {});

  const hasImages = attachments.some((a) => a.type === "image");
  const hasDocuments = attachments.some((a) => a.type === "document");

  return (
    <div className="px-4 pb-4 pt-2">
      <div className="max-w-3xl mx-auto relative">
        {/* Slash Command Menu */}
        {showSlashMenu && filteredCommands.length > 0 && (
          <SlashCommandMenu
            commands={filteredCommands}
            selectedIndex={selectedIndex}
            onSelect={handleSlashSelect}
          />
        )}

        {/* Voice status strip — state label + live partial transcript +
            inline error alert. Only visible while voice is active. */}
        <VoiceStatusStrip onInterrupt={onInterruptSpeaking} />

        {/* Input Container */}
        <div className="input-glow rounded-2xl border border-border bg-card transition-all">
          {/* Attachment Previews */}
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-3.5 pt-2.5">
              {attachments.map((att, i) => (
                <AttachmentPreview
                  key={`${att.file.name}-${i}`}
                  attachment={att}
                  onRemove={() => removeAttachment(i)}
                />
              ))}
            </div>
          )}

          <div className="flex items-end gap-1.5 px-3.5 py-2.5">
            {/* Hidden file inputs */}
            <input
              ref={imageInputRef}
              type="file"
              accept="image/*"
              multiple
              className="hidden"
              onChange={handleImageSelect}
            />
            <input
              ref={docInputRef}
              type="file"
              accept=".pdf,.txt,.md,.markdown,.csv,.tsv,.doc,.docx,.xls,.xlsx,.ppt,.pptx"
              multiple
              className="hidden"
              onChange={handleDocumentSelect}
            />

            {/* Attach button (opens image picker on click, document on right-click) */}
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-muted-foreground/50 hover:text-muted-foreground shrink-0"
              disabled={isStreaming}
              onClick={() => imageInputRef.current?.click()}
              onContextMenu={(e) => {
                e.preventDefault();
                docInputRef.current?.click();
              }}
              title="Attach image (right-click for documents)"
            >
              <Paperclip className="w-4.5 h-4.5" />
            </Button>

            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                hasImages
                  ? "Describe what you see in the image..."
                  : t("input.placeholder")
              }
              className="flex-1 resize-none bg-transparent text-foreground placeholder:text-muted-foreground/50 focus:outline-none min-h-6 max-h-45 py-1 leading-relaxed"
              rows={1}
              disabled={isStreaming}
            />

            {/* Web search toggle indicator */}
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-muted-foreground/50 hover:text-muted-foreground shrink-0"
              disabled={isStreaming}
              title="Web search is available automatically when needed"
            >
              <Globe className="w-4.5 h-4.5" />
            </Button>

            {/* Voice mic button — state-colored; while the assistant speaks,
                clicking it = interrupt. Never disabled by streaming. */}
            <VoiceMicButton
              voiceState={voiceState}
              disabled={voiceSetupDisabled}
              disabledReason={voiceDisabledReason}
              onToggle={onToggleVoice}
            />

            <Button
              onClick={handleSend}
              disabled={
                isStreaming || (!input.trim() && attachments.length === 0)
              }
              size="icon"
              className={cn(
                "h-8 w-8 rounded-xl shrink-0 transition-all",
                (input.trim() || attachments.length > 0) && !isStreaming
                  ? "bg-primary hover:bg-primary/90 text-primary-foreground"
                  : "bg-secondary text-muted-foreground/40",
              )}
            >
              {isStreaming ? (
                <Loader2 className="w-4.5 h-4.5 animate-spin" />
              ) : (
                <Send className="w-4.5 h-4.5" />
              )}
            </Button>
          </div>
        </div>

        {/* Model Selector + Disclaimer */}
        <div className="flex items-center justify-between mt-2 px-1">
          <div className="relative" ref={menuRef}>
            <button
              onClick={() => setShowModelMenu(!showModelMenu)}
              className="flex items-center gap-1.5 text-[12px] text-muted-foreground hover:text-foreground transition-colors"
              disabled={isStreaming}
            >
              <div className="w-4 h-4 rounded-full bg-linear-to-br from-indigo-500 to-blue-600 flex items-center justify-center">
                <span className="text-[7px] text-white font-bold">AI</span>
              </div>
              <span>{getSelectedModelLabel()}</span>
              {hasImages && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-400">
                  + vision
                </span>
              )}
              <ChevronDown className="w-3 h-3" />
            </button>
            {showModelMenu && (
              <div className="absolute bottom-full mb-2 left-0 w-56 rounded-xl border border-border bg-popover shadow-2xl z-50 max-h-70 overflow-y-auto">
                {Object.entries(modelGroups).map(([type, groupModels]) => (
                  <div key={type}>
                    <div className="px-3 py-1.5 text-[10px] font-semibold text-muted-foreground/60 uppercase tracking-wider">
                      {type}
                    </div>
                    {groupModels.map((m) => (
                      <button
                        key={m.id + m.role}
                        onClick={() => {
                          setSelectedModel(m.role || m.id);
                          setShowModelMenu(false);
                        }}
                        className={cn(
                          "w-full text-left px-3 py-2 text-[13px] hover:bg-accent transition-colors",
                          selectedModel === (m.role || m.id)
                            ? "bg-accent font-medium text-foreground"
                            : "text-muted-foreground",
                        )}
                      >
                        <div>{m.description}</div>
                        <div className="text-[10px] text-muted-foreground/60">
                          {m.id} · {m.size}
                        </div>
                      </button>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="flex items-center gap-2">
            {hasImages && (
              <span className="text-[10px] text-blue-400/60">
                📷 {attachments.filter((a) => a.type === "image").length} image
                {attachments.filter((a) => a.type === "image").length > 1
                  ? "s"
                  : ""}
              </span>
            )}
            {hasDocuments && (
              <span className="text-[10px] text-amber-400/60">
                📄 {attachments.filter((a) => a.type === "document").length} doc
                {attachments.filter((a) => a.type === "document").length > 1
                  ? "s"
                  : ""}
              </span>
            )}
            <span className="text-[10px] text-muted-foreground/40">
              {t("input.runningLocally")}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
