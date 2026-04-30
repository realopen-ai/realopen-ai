import { useState, useRef, useEffect } from "react";
import { Send, Loader2, Paperclip, Globe, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useChatStore } from "@/store/chatStore";
import { cn } from "@/lib/utils";

export function InputArea({
  onSend,
  isStreaming,
}: {
  onSend: (message: string) => void;
  isStreaming: boolean;
}) {
  const [input, setInput] = useState("");
  const models = useChatStore((s) => s.models);
  const selectedModel = useChatStore((s) => s.selectedModel);
  const setSelectedModel = useChatStore((s) => s.setSelectedModel);
  const [showModelMenu, setShowModelMenu] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

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

  const handleSend = () => {
    const trimmed = input.trim();
    if (!trimmed || isStreaming) return;
    onSend(trimmed);
    setInput("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
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
    const t = m.type || "other";
    if (!acc[t]) acc[t] = [];
    acc[t].push(m);
    return acc;
  }, {});

  return (
    <div className="px-4 pb-4 pt-2">
      <div className="max-w-3xl mx-auto">
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
              placeholder="Message RealOpen-AI..."
              className="flex-1 resize-none bg-transparent text-[15px] text-foreground placeholder:text-muted-foreground/50 focus:outline-none min-h-[24px] max-h-[180px] py-1 leading-relaxed"
              rows={1}
              disabled={isStreaming}
            />

            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-muted-foreground/50 hover:text-muted-foreground shrink-0"
              disabled={isStreaming}
            >
              <Globe className="w-[18px] h-[18px]" />
            </Button>

            <Button
              onClick={handleSend}
              disabled={isStreaming || !input.trim()}
              size="icon"
              className={cn(
                "h-8 w-8 rounded-xl shrink-0 transition-all",
                input.trim() && !isStreaming
                  ? "bg-[#6366f1] hover:bg-[#5558e6] text-white"
                  : "bg-secondary text-muted-foreground/40",
              )}
            >
              {isStreaming ? (
                <Loader2 className="w-[18px] h-[18px] animate-spin" />
              ) : (
                <Send className="w-[18px] h-[18px]" />
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
              <div className="absolute bottom-full mb-2 left-0 w-56 rounded-xl border border-border bg-popover shadow-2xl z-50 max-h-[280px] overflow-y-auto">
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
            Running locally via Ollama
          </span>
        </div>
      </div>
    </div>
  );
}
