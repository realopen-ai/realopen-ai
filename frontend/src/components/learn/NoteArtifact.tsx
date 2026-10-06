import { Link } from "react-router-dom";
import { FileText } from "lucide-react";
import { useT } from "@/store/settingsStore";
export function NoteArtifact({ id, title }: { id: string; title: string }) {
  const t = useT();
  return (
    <Link
      to={`/learn/notes/${id}`}
      className="my-3 flex items-center gap-3 rounded-xl border border-border/60 bg-card p-4 hover:border-primary/40 transition-colors"
    >
      <FileText className="size-5 text-muted-foreground shrink-0" />
      <div className="min-w-0">
        <p className="font-medium truncate">{title}</p>
        <p className="text-xs text-muted-foreground mt-1">
          {t("learn.openNote")}
        </p>
      </div>
    </Link>
  );
}
