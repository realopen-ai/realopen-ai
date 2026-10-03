import type { Conversation } from "@/store/chatStore";

/** A conversation is generating while any of its messages is still live. */
export function isConversationStreaming(
  conversation: Pick<Conversation, "messages">,
): boolean {
  return conversation.messages.some((message) => message.isStreaming);
}
