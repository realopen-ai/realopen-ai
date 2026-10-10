import { Link, useLocation, useNavigate } from "react-router-dom";
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
    <div className="my-3 border border-border rounded-xl p-4 space-y-3">
      <h3 dir="auto" className="font-medium">
        {title}
      </h3>
      <p className="text-sm text-muted-foreground">
        {count} {t("quiz.questions")}
      </p>
      <div className="flex gap-3 items-center">
        <Button size="sm" onClick={take}>
          {t("quiz.take")}
        </Button>
        <Link
          className="text-sm text-muted-foreground hover:text-primary"
          to={`/learn/quizzes/${id}`}
        >
          {t("quiz.open")}
        </Link>
      </div>
    </div>
  );
}
