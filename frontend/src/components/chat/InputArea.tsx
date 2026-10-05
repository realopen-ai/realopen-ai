import { useState, useRef, useEffect, useMemo } from "react";
import {
  Send,
  Paperclip,
  Globe,
  ChevronDown,
  Check,
  Cpu,
  Slash,
  Sparkles,
  Brain,
  SmilePlus,
  X,
  Image as ImageIcon,
  FileText,
  PhoneCall,
  Square,
  Mic,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from "@/components/ui/tooltip";
import { useChatStore } from "@/store/chatStore";
import { t } from "@/store/settingsStore";
import { useVoiceStore } from "@/voice/voiceStore";
import { cn } from "@/lib/utils";
import { DictationRecorder, appendDictation } from "@/voice/dictation";

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
            <span className="ml-auto flex items-center gap-1 text-[11px] text-muted-foreground/70">
              <kbd className="inline-flex items-center justify-center px-1.5 py-0.5 rounded bg-secondary border border-border/50 text-[10.5px] font-mono leading-none">
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
        <div className="w-8 h-8 rounded bg-secondary flex items-center justify-center shrink-0 text-muted-foreground">
          {isImage ? (
            <ImageIcon className="w-4 h-4" />
          ) : (
            <FileText className="w-4 h-4" />
          )}
        </div>
      )}
      <div className="min-w-0 flex-1">
        <p className="text-[11px] text-foreground truncate">
          {attachment.file.name}
        </p>
        <p className="text-[11px] text-muted-foreground/80">
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

// ─── Input Area ───────────────────────────────────────────────────

export function InputArea({
  onSend,
  isStreaming,
  onStartVoice,
  onStopResponse,
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
  /** Open the call surface and immediately connect/start listening. */
  onStartVoice: () => void;
  /** Stop the current text or voice response, preserving partial output. */
  onStopResponse: () => void;
}) {
  const [input, setInput] = useState("");
  const dictationConversation = useChatStore((s) => s.activeConversationId);
  const [dictation, setDictation] = useState<
    "idle" | "starting" | "recording" | "transcribing"
  >("idle");
  const [dictationError, setDictationError] = useState(false);
  const dictationRef = useRef<DictationRecorder | null>(null);
  const dictationAbort = useRef<AbortController | null>(null);
  const dictationTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dictationMounted = useRef(true);
  useEffect(() => {
    dictationMounted.current = true;
    setDictation("idle");
    setDictationError(false);
    return () => {
      dictationMounted.current = false;
      dictationRef.current?.dispose();
      dictationRef.current = null;
      dictationAbort.current?.abort();
      if (dictationTimer.current) clearTimeout(dictationTimer.current);
    };
  }, [dictationConversation]);
  const stopDictation = async () => {
    if (dictationTimer.current) clearTimeout(dictationTimer.current);
    setDictation("transcribing");
    const controller = new AbortController();
    const recorder = dictationRef.current;
    dictationAbort.current = controller;
    try {
      const pcm = await recorder!.stop();
      const response = await fetch("/api/voice/transcribe", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: pcm,
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("Transcription failed");
      const result = await response.json();
      if (dictationMounted.current && !controller.signal.aborted) {
        setInput((draft) => appendDictation(draft, String(result.text ?? "")));
        textareaRef.current?.focus();
      }
    } catch {
      if (dictationMounted.current && !controller.signal.aborted)
        setDictationError(true);
    } finally {
      recorder?.dispose();
      if (dictationMounted.current && dictationRef.current === recorder)
        setDictation("idle");
    }
  };
  const toggleDictation = async () => {
    if (dictation === "recording") return stopDictation();
    if (dictation !== "idle") return;
    setDictationError(false);
    setDictation("starting");
    const recorder = new DictationRecorder();
    dictationRef.current = recorder;
    try {
      await recorder.start();
      if (!dictationMounted.current || dictationRef.current !== recorder)
        return;
      setDictation("recording");
      dictationTimer.current = setTimeout(() => void stopDictation(), 55000);
    } catch {
      recorder.dispose();
      if (dictationMounted.current && dictationRef.current === recorder) {
        setDictationError(true);
        setDictation("idle");
      }
    }
  };
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
    if (dictation !== "idle") return;
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
    let desc: string | undefined;
    if (selectedModel === "default") {
      const m = models.find((m) => m.role === "default");
      desc = m?.description;
    } else {
      const m = models.find(
        (m) => m.id === selectedModel || m.role === selectedModel,
      );
      desc = m?.description;
    }
    // Concise collapsed label — the name only (e.g. "Qwen3.5 4B MLX").
    // Full description / id / size live inside the dropdown.
    if (desc) return desc.split("—")[0].split("(")[0].trim();
    return selectedModel === "default" ? "Default" : selectedModel;
  };

  const modelGroups = models.reduce<Record<string, typeof models>>((acc, m) => {
    const type = m.type || "other";
    if (!acc[type]) acc[type] = [];
    acc[type].push(m);
    return acc;
  }, {});

  const hasImages = attachments.some((a) => a.type === "image");

  return (
    <div className="px-4 pb-4 pt-12 sm:px-6">
      <div className="max-w-210 mx-auto relative">
        {voiceState === "inactive" && (
          <Button
            onClick={onStartVoice}
            disabled={voiceSetupDisabled || dictation !== "idle"}
            aria-label={voiceDisabledReason ?? t("voice.call.start")}
            title={voiceDisabledReason ?? t("voice.call.start")}
            className="absolute -top-11 right-0 h-9 w-9 rounded-full bg-primary text-primary-foreground shadow-lg cursor-pointer hover:bg-primary/90"
            size="icon-sm"
          >
            <PhoneCall className="h-4 w-4" />
          </Button>
        )}
        {(dictation !== "idle" || dictationError) && (
          <p
            role="status"
            aria-live="polite"
            className="mb-2 text-xs text-muted-foreground"
          >
            {dictationError
              ? t("input.dictation.error")
              : t(`input.dictation.${dictation}`)}
          </p>
        )}
        {/* Slash Command Menu */}
        {showSlashMenu && filteredCommands.length > 0 && (
          <SlashCommandMenu
            commands={filteredCommands}
            selectedIndex={selectedIndex}
            onSelect={handleSlashSelect}
          />
        )}

        {/* ── Composer — one cohesive floating input surface ── */}
        <div className="input-glow rounded-xl border border-border/70 bg-card shadow-[0_8px_30px_var(--color-shadow-soft)] transition-all">
          {/* Attachment Previews */}
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-3.5 pt-3">
              {attachments.map((att, i) => (
                <AttachmentPreview
                  key={`${att.file.name}-${i}`}
                  attachment={att}
                  onRemove={() => removeAttachment(i)}
                />
              ))}
            </div>
          )}

          {/* Textarea — comfortable padding, autosizing */}
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
            className="w-full resize-none bg-transparent text-foreground placeholder:text-muted-foreground/70 focus:outline-none px-4 pt-3.5 pb-1.5 min-h-13 max-h-45 leading-relaxed disabled:cursor-not-allowed disabled:opacity-60"
            style={{ fontSize: "var(--app-font-size)" }}
            rows={1}
            disabled={isStreaming}
            aria-label={t("input.placeholder")}
          />

          {/* Control row — inside the composer surface, no mini-borders */}
          <div className="flex items-center gap-1 px-2 pb-2.5 pt-1">
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
              size="icon-sm"
              className="text-muted-foreground/80 hover:text-muted-foreground shrink-0"
              disabled={isStreaming}
              onClick={() => imageInputRef.current?.click()}
              onContextMenu={(e) => {
                e.preventDefault();
                docInputRef.current?.click();
              }}
              aria-label="Attach image (right-click for documents)"
              title="Attach image (right-click for documents)"
            >
              <Paperclip className="h-4 w-4" />
            </Button>

            {/* Model selector — concise name + chevron, details in dropdown */}
            <div className="relative min-w-0 shrink" ref={menuRef}>
              <button
                onClick={() => setShowModelMenu(!showModelMenu)}
                className="flex items-center gap-1.5 h-7 rounded-md px-2 text-[12px] text-muted-foreground hover:text-foreground hover:bg-surface-hover/60 transition-colors max-w-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 disabled:opacity-50 disabled:pointer-events-none"
                disabled={isStreaming}
                aria-label="Select model"
                aria-expanded={showModelMenu}
              >
                <span className="flex h-3.5 w-3.5 items-center justify-center rounded-[5px] bg-primary/10 text-primary shrink-0">
                  <Cpu className="h-2.5 w-2.5" />
                </span>
                <span className="truncate">{getSelectedModelLabel()}</span>
                {hasImages && (
                  <span className="text-[11px] px-1.5 py-0.5 rounded bg-primary/10 text-primary shrink-0">
                    + vision
                  </span>
                )}
                <ChevronDown className="w-3 h-3 shrink-0" />
              </button>
              {showModelMenu && (
                <div className="absolute bottom-full mb-2 left-0 w-64 rounded-xl border border-border/60 bg-popover shadow-2xl z-50 max-h-70 overflow-y-auto py-1">
                  {Object.entries(modelGroups).map(([type, groupModels]) => (
                    <div key={type}>
                      <div className="px-3 pt-1.5 pb-1 text-[11px] font-semibold text-muted-foreground/80 uppercase tracking-wider">
                        {type}
                      </div>
                      {groupModels.map((m) => {
                        const isSelected = selectedModel === (m.role || m.id);
                        // Concise name (text before the "—" descriptor) as the
                        // primary line — full descriptor would truncate anyway.
                        const name = (m.description ?? m.id)
                          .split("—")[0]
                          .trim();
                        return (
                          <button
                            key={m.id + m.role}
                            onClick={() => {
                              setSelectedModel(m.role || m.id);
                              setShowModelMenu(false);
                            }}
                            className={cn(
                              "w-full text-left px-3 py-1.5 transition-colors",
                              isSelected
                                ? "bg-primary/10"
                                : "hover:bg-surface-hover/60",
                            )}
                          >
                            <div
                              className={cn(
                                "flex items-center gap-1.5 text-[12.5px]",
                                isSelected
                                  ? "font-medium text-foreground"
                                  : "text-foreground/90",
                              )}
                            >
                              <Check
                                className={cn(
                                  "w-3 h-3 shrink-0 text-primary",
                                  isSelected ? "opacity-100" : "opacity-0",
                                )}
                                aria-hidden="true"
                              />
                              <span className="truncate">{name}</span>
                            </div>
                            <div className="text-[10.5px] text-muted-foreground/70 truncate pl-4.5">
                              {m.id} · {m.size}
                            </div>
                          </button>
                        );
                      })}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <span className="flex-1" />

            {/* Web search indicator — available automatically when needed */}
            <Button
              variant="ghost"
              size="icon-sm"
              className="text-muted-foreground/80 hover:text-muted-foreground shrink-0"
              disabled={isStreaming}
              aria-label="Web search is available automatically when needed"
              title="Web search is available automatically when needed"
            >
              <Globe className="h-4 w-4" />
            </Button>

            {/* Voice — starts a voice call from the composer */}
            {voiceState === "inactive" && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => void toggleDictation()}
                    disabled={
                      isStreaming ||
                      dictation === "starting" ||
                      dictation === "transcribing" ||
                      readiness?.enabled === false
                    }
                    aria-label={
                      dictation === "recording"
                        ? t("input.dictation.stop")
                        : t("input.dictation.start")
                    }
                    className={cn(
                      "shrink-0",
                      dictation === "recording"
                        ? "text-destructive animate-pulse"
                        : "text-muted-foreground/80",
                    )}
                  >
                    {dictation === "starting" ||
                    dictation === "transcribing" ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : dictation === "recording" ? (
                      <Square className="h-4 w-4" />
                    ) : (
                      <Mic className="h-4 w-4" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="top">
                  {dictation === "recording"
                    ? t("input.dictation.stop")
                    : t("input.dictation.start")}
                </TooltipContent>
              </Tooltip>
            )}

            {/* Send / stop generation */}
            <Button
              onClick={isStreaming ? onStopResponse : handleSend}
              disabled={
                !isStreaming &&
                (dictation !== "idle" ||
                  (!input.trim() && attachments.length === 0))
              }
              aria-label={
                isStreaming ? t("input.stopResponse") : t("input.send")
              }
              title={isStreaming ? t("input.stopResponse") : t("input.send")}
              size="icon"
              className={cn(
                "h-9 w-9 rounded-full shrink-0 transition-all",
                isStreaming
                  ? "bg-secondary border border-border/70 text-foreground hover:bg-surface-hover"
                  : input.trim() || attachments.length > 0
                    ? "bg-primary hover:bg-primary/90 text-primary-foreground shadow-sm"
                    : "bg-secondary text-muted-foreground/70 hover:bg-secondary disabled:opacity-100",
              )}
            >
              {isStreaming ? (
                <Square className="w-3 h-3 fill-current" />
              ) : (
                <Send className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>

        {/* Quiet disclaimer below the surface */}
        <p className="text-center text-[11px] text-muted-foreground/70 mt-2 select-none">
          {t("input.runningLocally")}
        </p>
      </div>
    </div>
  );
}
