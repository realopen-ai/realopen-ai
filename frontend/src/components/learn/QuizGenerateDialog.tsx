import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { quizzesApi } from "@/api/quizzesClient";
import { useT } from "@/store/settingsStore";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { fieldClass } from "./LearnDialogs";

export function QuizGenerateDialog({
  noteId,
  selection,
  onClose,
}: {
  noteId: string;
  selection?: string;
  onClose: () => void;
}) {
  const t = useT();
  const navigate = useNavigate();
  const [count, setCount] = useState(5);
  const [type, setType] = useState("mixed");
  const [difficulty, setDifficulty] = useState("mixed");
  const [language, setLanguage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("quiz.generate")}</DialogTitle>
        </DialogHeader>
        <label>
          {t("quiz.questions")}
          <input
            className={fieldClass}
            type="number"
            min={1}
            max={20}
            value={count}
            onChange={(e) => setCount(Number(e.target.value))}
          />
        </label>
        <label>
          {t("quiz.type")}
          <select
            className={fieldClass}
            value={type}
            onChange={(e) => setType(e.target.value)}
          >
            {["mixed", "multiple_choice", "short_answer"].map((v) => (
              <option key={v} value={v}>
                {t(
                  v === "mixed"
                    ? "quiz.mixed"
                    : v === "multiple_choice"
                      ? "quiz.multipleChoice"
                      : "quiz.shortAnswer",
                )}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("quiz.difficulty")}
          <select
            className={fieldClass}
            value={difficulty}
            onChange={(e) => setDifficulty(e.target.value)}
          >
            {["mixed", "easy", "medium", "hard"].map((v) => (
              <option key={v} value={v}>
                {t(`quiz.${v}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("quiz.language")}
          <input
            dir="auto"
            className={fieldClass}
            value={language}
            maxLength={80}
            onChange={(e) => setLanguage(e.target.value)}
            placeholder={t("quiz.sourceLanguage")}
          />
        </label>
        {error && (
          <p role="alert" className="text-destructive">
            {error}
          </p>
        )}
        <Button
          disabled={busy || count < 1 || count > 20}
          onClick={async () => {
            setBusy(true);
            try {
              const quiz = await quizzesApi.fromNote(noteId, {
                count,
                type,
                difficulty,
                language,
                selection,
              });
              navigate(`/learn/quizzes/${quiz.id}`);
              onClose();
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          {t(busy ? "quiz.creating" : "quiz.generate")}
        </Button>
      </DialogContent>
    </Dialog>
  );
}
