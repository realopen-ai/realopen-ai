import { useState, useRef, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Send, Bot, User, Loader2, ChevronDown } from "lucide-react";

interface ModelOption {
  id: string;
  type: string;
  role: string;
  description: string;
  size: string;
}

interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  model?: string;
}

function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>("default");
  const [showModelMenu, setShowModelMenu] = useState(false);
  const [profileName, setProfileName] = useState<string>("");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Fetch available models on mount
  useEffect(() => {
    fetch("/api/profile/models")
      .then((res) => res.json())
      .then((data) => {
        if (data.models) {
          setModels(data.models);
          setProfileName(data.profile || "");
        }
      })
      .catch(() => {
        // Fallback if API not available
        setModels([
          {
            id: "qwen3:4b",
            type: "chat",
            role: "default",
            description: "Default chat model",
            size: "",
          },
        ]);
      });
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const sendMessage = async () => {
    const trimmed = input.trim();
    if (!trimmed || isStreaming) return;

    setError(null);
    const userMessage: Message = {
      role: "user",
      content: trimmed,
      model: selectedModel,
    };
    const updatedMessages = [...messages, userMessage];
    setMessages(updatedMessages);
    setInput("");
    setIsStreaming(true);

    const assistantMessage: Message = { role: "assistant", content: "" };
    setMessages([...updatedMessages, assistantMessage]);

    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: updatedMessages.map((m) => ({
            role: m.role,
            content: m.content,
          })),
          model: selectedModel, // Sends role/type/ID - backend resolves it
          stream: true,
        }),
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();

      if (!reader) throw new Error("No response body");

      let accumulated = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split("\n");

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const data = line.slice(6).trim();
            if (data === "[DONE]") continue;

            try {
              const parsed = JSON.parse(data);
              if (parsed.message?.content) {
                accumulated += parsed.message.content;
                setMessages((prev) => {
                  const newMessages = [...prev];
                  newMessages[newMessages.length - 1] = {
                    role: "assistant",
                    content: accumulated,
                  };
                  return newMessages;
                });
              }
              if (parsed.error) {
                setError(parsed.error);
              }
            } catch {
              // Skip malformed JSON chunks
            }
          }
        }
      }
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : "Unknown error";
      setError(`Failed to get response: ${errorMsg}`);
      setMessages((prev) => {
        const newMessages = [...prev];
        newMessages[newMessages.length - 1] = {
          role: "assistant",
          content:
            "Sorry, I encountered an error. Please make sure Ollama is running.",
        };
        return newMessages;
      });
    } finally {
      setIsStreaming(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  // Get a display label for the selected model
  const getSelectedModelLabel = () => {
    if (selectedModel === "default") {
      const defaultModel = models.find((m) => m.role === "default");
      return defaultModel ? defaultModel.description : "Default";
    }
    const model = models.find(
      (m) => m.id === selectedModel || m.role === selectedModel,
    );
    return model ? model.description : selectedModel;
  };

  // Group models by type for the dropdown
  const modelGroups = models.reduce<Record<string, ModelOption[]>>((acc, m) => {
    const type = m.type || "other";
    if (!acc[type]) acc[type] = [];
    acc[type].push(m);
    return acc;
  }, {});

  return (
    <div className="flex h-screen bg-background">
      {/* Sidebar */}
      <aside className="hidden md:flex w-64 flex-col border-r bg-card">
        <div className="p-4 border-b">
          <h1 className="text-xl font-bold">RealOpen-AI</h1>
          <p className="text-xs text-muted-foreground mt-1">
            Local &amp; Private AI
          </p>
          {profileName && (
            <p className="text-xs text-muted-foreground">
              Profile: {profileName}
            </p>
          )}
        </div>
        <nav className="flex-1 p-2">
          <button
            onClick={() => {
              setMessages([]);
              setError(null);
            }}
            className="w-full text-left px-3 py-2 rounded-md text-sm hover:bg-accent transition-colors"
          >
            + New Chat
          </button>
        </nav>
        <div className="p-4 border-t text-xs text-muted-foreground">
          100% Offline &amp; Private
        </div>
      </aside>

      {/* Main Chat Area */}
      <main className="flex-1 flex flex-col">
        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {messages.length === 0 && (
            <div className="flex items-center justify-center h-full">
              <div className="text-center space-y-3">
                <Bot className="h-12 w-12 mx-auto text-muted-foreground" />
                <h2 className="text-2xl font-semibold">RealOpen-AI</h2>
                <p className="text-muted-foreground max-w-md">
                  Your fully offline, private AI assistant. Start a conversation
                  below.
                </p>
              </div>
            </div>
          )}

          {messages.map((msg, i) => (
            <div
              key={i}
              className={`flex gap-3 ${msg.role === "user" ? "justify-end" : "justify-start"}`}
            >
              {msg.role === "assistant" && (
                <div className="flex-shrink-0 w-8 h-8 rounded-full bg-primary text-primary-foreground flex items-center justify-center">
                  <Bot className="h-4 w-4" />
                </div>
              )}
              <div
                className={`max-w-[80%] rounded-lg px-4 py-2 ${
                  msg.role === "user"
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted"
                }`}
              >
                {msg.role === "assistant" ? (
                  <div className="prose prose-sm dark:prose-invert max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {msg.content ||
                        (isStreaming && i === messages.length - 1 ? "..." : "")}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
                )}
              </div>
              {msg.role === "user" && (
                <div className="flex-shrink-0 w-8 h-8 rounded-full bg-secondary flex items-center justify-center">
                  <User className="h-4 w-4" />
                </div>
              )}
            </div>
          ))}

          {error && (
            <div className="text-center text-sm text-destructive bg-destructive/10 rounded-md px-4 py-2">
              {error}
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input Area */}
        <div className="border-t p-4">
          <div className="flex gap-2 max-w-3xl mx-auto">
            {/* Model Selector */}
            <div className="relative flex-shrink-0">
              <button
                onClick={() => setShowModelMenu(!showModelMenu)}
                className="flex items-center gap-1 rounded-lg border bg-background px-3 py-2 text-sm hover:bg-accent transition-colors h-[44px]"
                disabled={isStreaming}
              >
                <span className="max-w-[120px] truncate">
                  {getSelectedModelLabel()}
                </span>
                <ChevronDown className="h-3 w-3 flex-shrink-0" />
              </button>
              {showModelMenu && (
                <div className="absolute bottom-full mb-1 left-0 w-64 rounded-lg border bg-popover shadow-lg z-50 max-h-[300px] overflow-y-auto">
                  {Object.entries(modelGroups).map(([type, groupModels]) => (
                    <div key={type}>
                      <div className="px-3 py-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider border-b">
                        {type}
                      </div>
                      {groupModels.map((m) => (
                        <button
                          key={m.id}
                          onClick={() => {
                            setSelectedModel(m.role || m.id);
                            setShowModelMenu(false);
                          }}
                          className={`w-full text-left px-3 py-2 text-sm hover:bg-accent transition-colors ${
                            selectedModel === (m.role || m.id)
                              ? "bg-accent font-medium"
                              : ""
                          }`}
                        >
                          <div>{m.description}</div>
                          <div className="text-xs text-muted-foreground">
                            {m.id} · {m.size}
                          </div>
                        </button>
                      ))}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type a message... (Enter to send, Shift+Enter for newline)"
              className="flex-1 resize-none rounded-lg border bg-transparent px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring min-h-[44px] max-h-[200px]"
              rows={1}
              disabled={isStreaming}
            />
            <button
              onClick={sendMessage}
              disabled={isStreaming || !input.trim()}
              className="flex-shrink-0 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {isStreaming ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Send className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;
