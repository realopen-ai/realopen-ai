import { Link, useLocation, useNavigate } from "react-router-dom";
import { BookOpen, Play } from "lucide-react";
import { useT } from "@/store/settingsStore";
import { useLearnStore } from "@/store/learnStore";
import { useUIStore } from "@/store/uiStore";
import { Button } from "@/components/ui/button";

export function QuizArtifact({
  id,
  title,
  count,
}: {
  id: string;
  title: string;
  count: number;
}) {
  const t = useT();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  function take() {
    const notebook = pathname.match(/^\/learn\/notebooks\/([^/]+)$/);
    if (notebook) {
      window.dispatchEvent(
        new CustomEvent("notebook-quiz", {
          detail: { notebookId: notebook[1], quizId: id },
        }),
      );
      return;
    }
    if (window.innerWidth >= 768) {
      useLearnStore.getState().setQuiz(id);
      useUIStore.getState().setRightPanelOpen(true);
    } else navigate(`/learn/quizzes/${id}/take`);
  }
  return (
    <div className="my-3 max-w-md rounded-xl border border-border/60 bg-card p-4">
      <div className="flex gap-3 items-center">
        <BookOpen aria-hidden="true" className="size-5 text-primary shrink-0" />
        <div className="min-w-0">
          <h3 dir="auto" className="font-medium truncate">
            {title}
          </h3>
          <p className="text-xs text-muted-foreground">
            {count} {t("quiz.questions")}
          </p>
        </div>
      </div>
      <div className="flex gap-2 mt-3">
        <Button size="sm" onClick={take}>
          <Play aria-hidden="true" className="size-3.5" />
          {t("quiz.take")}
        </Button>
        <Button size="sm" variant="outline" asChild>
          <Link to={`/learn/quizzes/${id}`}>{t("quiz.open")}</Link>
        </Button>
      </div>
    </div>
  );
}
