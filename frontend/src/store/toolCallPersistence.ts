import type { Sandbox } from "@/store/sandboxStore";

const DETAIL_FIELDS = [
  "agent",
  "toolName",
  "parentId",
  "filePath",
  "fileContent",
  "skillName",
  "skillResource",
  "diff",
  "previewUrl",
  "previewPort",
  "sandboxId",
  "sandbox",
] as const;

type PersistedToolCall = {
  agent?: "coder";
  toolName?: string;
  parentId?: string;
  filePath?: string;
  fileContent?: string;
  skillName?: string;
  skillResource?: string;
  diff?: string;
  previewUrl?: string;
  previewPort?: number;
  sandboxId?: string;
  sandbox?: Sandbox;
  unrelated?: string;
};

/** Preserve nested coder details when rebuilding a message from JSONB. */
export function persistedToolCallDetails(
  toolCall: PersistedToolCall,
): PersistedToolCall {
  return Object.fromEntries(
    DETAIL_FIELDS.flatMap((field) =>
      toolCall[field] === undefined ? [] : [[field, toolCall[field]]],
    ),
  ) as PersistedToolCall;
}
