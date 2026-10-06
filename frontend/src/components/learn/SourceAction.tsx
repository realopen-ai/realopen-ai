import { Link } from "react-router-dom";
import { ExternalLink } from "lucide-react";
import { useT } from "@/store/settingsStore";

export function SourceAction({
  documentId,
  conversationId,
  page,
  chunk,
}: {
  documentId?: string | null;
  conversationId?: string | null;
  page?: number | null;
  chunk?: string | null;
}) {
  const t = useT();
  const query = new URLSearchParams();
  if (documentId) query.set("document", documentId);
  if (page) query.set("page", String(page));
  if (chunk) query.set("chunk", chunk);
  const href = documentId
    ? `/workspace/documents?${query}`
    : conversationId
      ? `/${conversationId}`
      : null;
  return href ? (
    <Link
      to={href}
      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-primary"
    >
      <ExternalLink className="size-3" />
      {t("learn.viewSource")}
      {page ? ` · ${page}` : ""}
    </Link>
  ) : null;
}
