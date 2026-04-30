import type { ModelOption } from "@/store/chatStore";

export async function fetchModels(): Promise<{
  models: ModelOption[];
  profile: string;
}> {
  try {
    const res = await fetch("/api/profile/models");
    if (res.ok) return await res.json();
  } catch {
    /* fallback below */
  }
  return {
    profile: "16gb",
    models: [
      {
        id: "qwen3:8b",
        type: "chat",
        role: "default",
        description: "Qwen3 8B (Default)",
        size: "5.2 GB",
      },
      {
        id: "qwen3:8b",
        type: "chat",
        role: "fast",
        description: "Qwen3 8B (Fast)",
        size: "5.2 GB",
      },
      {
        id: "moondream:1.8b",
        type: "vision",
        role: "vision",
        description: "Moondream 1.8B",
        size: "1.7 GB",
      },
      {
        id: "deepseek-r1:7b",
        type: "coder",
        role: "coder",
        description: "DeepSeek-R1 7B",
        size: "9.8 GB",
      },
      {
        id: "qwen3:8b",
        type: "reasoning",
        role: "reasoning",
        description: "Qwen3 8B",
        size: "5.2 GB",
      },
    ],
  };
}
