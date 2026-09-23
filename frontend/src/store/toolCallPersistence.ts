const DETAIL_FIELDS = [
  "filePath",
  "fileContent",
  "diff",
  "previewUrl",
  "previewPort",
  "sandboxId",
] as const;

type PersistedToolCall = {
  filePath?: string;
  fileContent?: string;
  diff?: string;
  previewUrl?: string;
  previewPort?: number;
  sandboxId?: string;
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
