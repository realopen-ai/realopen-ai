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
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useChatStore } from "@/store/chatStore";
import { t } from "@/store/settingsStore";
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
    modelOverride: "default_imagine",
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
    <div className="absolute bottom-full left-0 right-0 mb-2 max-w-3xl mx-auto z-50">
      <div className="bg-popover border border-border rounded-xl shadow-2xl overflow-hidden">
        <div className="px-3 py-2 border-b border-border/50">
          <div className="flex items-center gap-1.5 text-muted-foreground">
            <Slash className="w-3.5 h-3.5" />
            <span className="text-[12px] font-medium">{t("input.slash")}</span>
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
}: {
  onSend: (
    message: string,
    options?: { modelOverride?: string; shrug?: boolean },
  ) => void;
  isStreaming: boolean;
}) {
  const [input, setInput] = useState("");
  const [showSlashMenu, setShowSlashMenu] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const models = useChatStore((s) => s.models);
  const selectedModel = useChatStore((s) => s.selectedModel);
  const setSelectedModel = useChatStore((s) => s.setSelectedModel);
  const [showModelMenu, setShowModelMenu] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // ── Slash menu filtering ──
  const slashFilter = useMemo(() => {
    const trimmed = input.trimStart();
    if (!trimmed.startsWith("/")) return "";
    // Extract the command part (first word)
    const firstWord = trimmed.split(/\s/)[0].slice(1); // remove /
    // If there's a space after the command, don't filter (command already typed)
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
    // Replace the /command part with just the command prefix (keep any text after)
    const trimmed = input.trimStart();
    const parts = trimmed.split(/\s+/);
    const afterCommand = parts.slice(1).join(" ");

    if (cmd.shrug) {
      // For /shrug, replace the entire slash part
      setInput(afterCommand);
    } else {
      // For /think, /imagine — replace with the command indicator then keep text
      setInput(afterCommand);
    }
    setShowSlashMenu(false);

    // Focus back on textarea
    setTimeout(() => textareaRef.current?.focus(), 0);
  };

  const handleSend = () => {
    const trimmed = input.trim();
    if (!trimmed || isStreaming) return;

    const { command, remainingContent } = parseSlashCommand(trimmed);
    const contentToSend = command
      ? remainingContent || trimmed.replace(/^\/\S+\s*/, "").trim() || trimmed
      : trimmed;

    // If there's a slash command with no remaining content, send the raw input for /shrug
    const finalContent = command?.shrug
      ? remainingContent || "¯\\_(ツ)_/¯"
      : contentToSend;

    onSend(finalContent || trimmed, {
      modelOverride: command?.modelOverride,
      shrug: command?.shrug,
    });
    setInput("");
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

        {/* Input Container */}
        <div className="input-glow rounded-2xl border border-border bg-card transition-all">
          <div className="flex items-end gap-1.5 px-3.5 py-2.5">
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-muted-foreground/50 hover:text-muted-foreground shrink-0"
              disabled={isStreaming}
            >
              <Paperclip className="w-4.5 h-4.5" />
            </Button>

            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={t("input.placeholder")}
              className="flex-1 resize-none bg-transparent text-foreground placeholder:text-muted-foreground/50 focus:outline-none min-h-6 max-h-45 py-1 leading-relaxed"
              rows={1}
              disabled={isStreaming}
            />

            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-muted-foreground/50 hover:text-muted-foreground shrink-0"
              disabled={isStreaming}
            >
              <Globe className="w-4.5 h-4.5" />
            </Button>

            <Button
              onClick={handleSend}
              disabled={isStreaming || !input.trim()}
              size="icon"
              className={cn(
                "h-8 w-8 rounded-xl shrink-0 transition-all",
                input.trim() && !isStreaming
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
          <span className="text-[10px] text-muted-foreground/40">
            {t("input.runningLocally")}
          </span>
        </div>
      </div>
    </div>
  );
}
