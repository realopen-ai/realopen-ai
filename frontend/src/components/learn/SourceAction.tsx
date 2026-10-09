import { Link, useLocation } from "react-router-dom";
import { ExternalLink } from "lucide-react";
import { useT } from "@/store/settingsStore";
import { useNotebookStore } from "@/store/notebookStore";
import {
  openNotebookSource,
  isNotebookSourceRoute,
} from "@/lib/sourceNavigation";
import {
  artifactSourceUrl,
  type ArtifactReference,
} from "@/api/artifactsClient";

export function SourceAction({
  documentId,
  conversationId,
  page,
  chunk,
  artifact,
  excerpt,
  title,
}: {
  documentId?: string | null;
  conversationId?: string | null;
  page?: number | null;
  chunk?: string | null;
  artifact?: ArtifactReference | null;
  excerpt?: string;
  title?: string;
}) {
  const t = useT();
  const notebook = useNotebookStore((s) => s.notebook);
  const { pathname } = useLocation();
  const query = new URLSearchParams();
  if (documentId) query.set("document", documentId);
  if (page) query.set("page", String(page));
  if (chunk) query.set("chunk", chunk);
  const href = artifact
    ? artifactSourceUrl(artifact)
    : documentId
      ? `/workspace/documents?${query}`
      : conversationId
        ? `/${conversationId}`
        : null;
  return href ? (
    <Link
      to={href}
      onClick={(event) => {
        if (
          notebook &&
          isNotebookSourceRoute(pathname, notebook.id) &&
          (artifact || documentId) &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey
        ) {
          event.preventDefault();
          openNotebookSource(notebook.id, {
            documentId,
            artifact,
            page,
            chunk,
            excerpt,
            title,
          });
        }
      }}
      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-primary"
    >
      <ExternalLink className="size-3" />
      {t("learn.viewSource")}
      {page ? ` · ${page}` : ""}
    </Link>
  ) : null;
}
