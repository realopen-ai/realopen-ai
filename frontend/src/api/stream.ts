import type { ToolCallResult } from "@/store/chatStore";

export interface StreamCallbacks {
  onToken: (token: string) => void;
  onToolCallStart: (
    toolCall: Omit<ToolCallResult, "id" | "startedAt">,
  ) => string;
  onToolCallUpdate: (
    toolCallId: string,
    updates: Partial<ToolCallResult>,
  ) => void;
  onDone: () => void;
  onError: (error: string) => void;
}

export async function streamChat(
  messages: { role: string; content: string }[],
  model: string,
  callbacks: StreamCallbacks,
): Promise<void> {
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages, model, stream: true }),
    });

    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const reader = response.body?.getReader();
    if (!reader) throw new Error("No response body");

    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const data = line.slice(6).trim();
        if (data === "[DONE]") {
          callbacks.onDone();
          return;
        }
        try {
          const parsed = JSON.parse(data);
          if (parsed.message?.content) {
            callbacks.onToken(parsed.message.content);
          }
          if (parsed.tool_call) {
            const tc = parsed.tool_call;
            if (tc.status === "running") {
              const id = callbacks.onToolCallStart({
                type: tc.type,
                status: "running",
                title: tc.title ?? tc.type,
                query: tc.query,
                language: tc.language,
                code: tc.code,
                steps: tc.steps,
              });
              // Auto-complete after a delay for demo
              if (tc.type === "websearch") {
                setTimeout(() => {
                  callbacks.onToolCallUpdate(id, {
                    status: "completed",
                    completedAt: Date.now(),
                    results: tc.results ?? [
                      {
                        title: "Search Result 1",
                        url: "https://example.com/1",
                        snippet: "Relevant information found...",
                      },
                      {
                        title: "Search Result 2",
                        url: "https://example.com/2",
                        snippet: "Additional context and data...",
                      },
                      {
                        title: "Search Result 3",
                        url: "https://example.com/3",
                        snippet: "More detailed findings...",
                      },
                    ],
                  });
                }, 1500);
              } else if (tc.type === "code_exec") {
                setTimeout(() => {
                  callbacks.onToolCallUpdate(id, {
                    status: "completed",
                    completedAt: Date.now(),
                    output:
                      tc.output ??
                      "Process completed successfully.\nOutput: 42",
                    exitCode: 0,
                  });
                }, 2000);
              } else if (tc.type === "deepsearch") {
                setTimeout(() => {
                  callbacks.onToolCallUpdate(id, {
                    status: "completed",
                    completedAt: Date.now(),
                    steps: tc.steps ?? [
                      { label: "Searching web", status: "done" },
                      { label: "Analyzing results", status: "done" },
                      { label: "Synthesizing answer", status: "done" },
                    ],
                  });
                }, 3000);
              }
            }
          }
          if (parsed.error) {
            callbacks.onError(parsed.error);
          }
        } catch {
          /* skip malformed JSON */
        }
      }
    }
    callbacks.onDone();
  } catch (err) {
    const errorMsg = err instanceof Error ? err.message : "Unknown error";
    callbacks.onError(errorMsg);
  }
}

// Demo mode — generates mock streaming responses with tool calls
export async function streamChatDemo(
  userMessage: string,
  callbacks: StreamCallbacks,
): Promise<void> {
  const lowerMsg = userMessage.toLowerCase();

  // Simulate tool calls based on keywords
  const hasSearch =
    lowerMsg.includes("search") ||
    lowerMsg.includes("find") ||
    lowerMsg.includes("look up") ||
    lowerMsg.includes("what is") ||
    lowerMsg.includes("who is") ||
    lowerMsg.includes("latest");
  const hasCode =
    lowerMsg.includes("code") ||
    lowerMsg.includes("python") ||
    lowerMsg.includes("script") ||
    lowerMsg.includes("calculate") ||
    lowerMsg.includes("run") ||
    lowerMsg.includes("write a");
  const hasDeep =
    lowerMsg.includes("deep research") ||
    lowerMsg.includes("research") ||
    lowerMsg.includes("analyze") ||
    lowerMsg.includes("compare");

  const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

  if (hasSearch) {
    const tcId = callbacks.onToolCallStart({
      type: "websearch",
      status: "running",
      title: "Searching the web",
      query: userMessage,
    });
    await delay(1200);
    callbacks.onToolCallUpdate(tcId, {
      status: "completed",
      completedAt: Date.now(),
      results: [
        {
          title: "Wikipedia - Related Topic",
          url: "https://en.wikipedia.org/wiki/Example",
          snippet:
            "Comprehensive overview of the topic with historical context and current developments...",
        },
        {
          title: "Recent Research Paper (2025)",
          url: "https://arxiv.org/abs/example",
          snippet:
            "Novel approach demonstrating significant improvements in accuracy and efficiency...",
        },
        {
          title: "Technical Documentation",
          url: "https://docs.example.com/guide",
          snippet:
            "Official documentation covering installation, configuration, and best practices...",
        },
      ],
    });
  }

  if (hasDeep) {
    const tcId = callbacks.onToolCallStart({
      type: "deepsearch",
      status: "running",
      title: "Deep research",
      steps: [
        { label: "Searching multiple sources", status: "running" },
        { label: "Analyzing results", status: "pending" },
        { label: "Cross-referencing data", status: "pending" },
        { label: "Synthesizing answer", status: "pending" },
      ],
    });
    await delay(1000);
    callbacks.onToolCallUpdate(tcId, {
      steps: [
        { label: "Searching multiple sources", status: "done" },
        { label: "Analyzing results", status: "running" },
        { label: "Cross-referencing data", status: "pending" },
        { label: "Synthesizing answer", status: "pending" },
      ],
    });
    await delay(1000);
    callbacks.onToolCallUpdate(tcId, {
      steps: [
        { label: "Searching multiple sources", status: "done" },
        { label: "Analyzing results", status: "done" },
        { label: "Cross-referencing data", status: "running" },
        { label: "Synthesizing answer", status: "pending" },
      ],
    });
    await delay(800);
    callbacks.onToolCallUpdate(tcId, {
      status: "completed",
      completedAt: Date.now(),
      steps: [
        { label: "Searching multiple sources", status: "done" },
        { label: "Analyzing results", status: "done" },
        { label: "Cross-referencing data", status: "done" },
        { label: "Synthesizing answer", status: "done" },
      ],
    });
  }

  if (hasCode) {
    const codeSnippet = lowerMsg.includes("python")
      ? `import numpy as np\n\ndata = np.random.randn(1000)\nmean = np.mean(data)\nstd = np.std(data)\nprint(f"Mean: {mean:.4f}")\nprint(f"Std:  {std:.4f}")`
      : `const result = Array.from({length: 1000}, () => Math.random());\nconst mean = result.reduce((a,b) => a+b, 0) / result.length;\nconsole.log(\`Mean: \${mean.toFixed(4)}\`);`;

    const tcId = callbacks.onToolCallStart({
      type: "code_exec",
      status: "running",
      title: "Running code",
      language: lowerMsg.includes("python") ? "python" : "javascript",
      code: codeSnippet,
    });
    await delay(1500);
    callbacks.onToolCallUpdate(tcId, {
      status: "completed",
      completedAt: Date.now(),
      output: `Mean: 0.0234\nStd:  1.0012\n\nProcess exited with code 0`,
      exitCode: 0,
    });
  }

  // Stream the text response
  const responses: Record<string, string> = {
    default:
      "I'd be happy to help you with that! Let me think about your question and provide a comprehensive answer.\n\nBased on my analysis, here are the key points to consider:\n\n1. **Context matters** — The approach you take should depend on your specific requirements and constraints.\n\n2. **Best practices** — Following established patterns and conventions will help ensure maintainability and reliability.\n\n3. **Testing** — Always verify your assumptions with real-world testing and validation.\n\nWould you like me to elaborate on any of these points?",
    search:
      "I searched the web for you and found several relevant results. Here's a summary of what I found:\n\nThe most up-to-date information suggests that this topic has seen significant developments recently. Key findings include:\n\n- **Recent advances** have improved performance by approximately 30%\n- **New methodologies** are being adopted across the industry\n- **Community consensus** is forming around best practices\n\nCheck the search results in the sandbox panel for more details.",
    code: "I've written and executed the code for you. Here's what happened:\n\nThe code ran successfully and produced the expected output. You can see the full execution details in the sandbox panel.\n\n**Key observations:**\n- The calculation completed without errors\n- Results are statistically significant\n- The approach scales well with larger datasets\n\nWould you like me to modify the code or run additional analysis?",
    deep: "After conducting deep research across multiple sources, here's a comprehensive analysis:\n\n## Summary\nThe topic has been extensively studied, with several key papers and developments in recent years.\n\n## Key Findings\n1. **Performance**: Modern approaches show 2-3x improvement over traditional methods\n2. **Efficiency**: Resource consumption has decreased by ~40% while maintaining quality\n3. **Accessibility**: New tools and frameworks have lowered the barrier to entry\n\n## Comparison\n| Method | Accuracy | Speed | Cost |\n|--------|----------|-------|------|\n| Traditional | 78% | Slow | High |\n| Modern | 94% | Fast | Medium |\n| Hybrid | 96% | Medium | Low |\n\nThe research steps are shown in the sandbox panel for transparency.",
  };

  let responseKey = "default";
  if (hasDeep) responseKey = "deep";
  else if (hasSearch && hasCode) responseKey = "deep";
  else if (hasSearch) responseKey = "search";
  else if (hasCode) responseKey = "code";

  const response = responses[responseKey];
  const words = response.split(" ");

  for (let i = 0; i < words.length; i++) {
    callbacks.onToken(i === 0 ? words[i] : " " + words[i]);
    await delay(30 + Math.random() * 40);
  }

  callbacks.onDone();
}
