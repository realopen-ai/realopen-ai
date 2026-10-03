export type ResponseOrigin = "text" | "voice";
export type CompletionAction = "none" | "toast" | "browser";

export function isViewingConversation(
  pathname: string,
  conversationId: string,
  activeConversationId?: string | null,
): boolean {
  return (
    pathname === `/${encodeURIComponent(conversationId)}` ||
    (pathname === "/" && activeConversationId === conversationId)
  );
}

export function completionAction(input: {
  origin: ResponseOrigin;
  appActive: boolean;
  viewingConversation: boolean;
}): CompletionAction {
  if (input.origin === "voice" || input.viewingConversation) return "none";
  return input.appActive ? "toast" : "browser";
}
