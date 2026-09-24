/** A null active conversation can mean a newly-created chat is still streaming. */
export function shouldLoadConversationWorkspace(
  conversationId: string | null,
): conversationId is string {
  return Boolean(conversationId);
}
