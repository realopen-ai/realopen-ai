import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useLocation } from "react-router-dom";
import { Plus, Pencil, Trash2 } from "lucide-react";
import {
  quizzesApi,
  type Quiz,
  type QuizQuestion,
  type QuizAttempt,
} from "@/api/quizzesClient";
import { useT } from "@/store/settingsStore";
import { Button } from "@/components/ui/button";
import { PageContainer, PageHeader } from "@/components/ui/primitives";
import { LearnNav } from "./NotesPage";
import { fieldClass, ConfirmDelete } from "./LearnDialogs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { SourceAction } from "./SourceAction";
import { QuizSession } from "./QuizSession";

const blank = (): QuizQuestion => ({
  id: crypto.randomUUID(),
  type: "short_answer",
  prompt: "",
  answer: "",
  options: [],
  explanation: "",
});

function QuizEditor({
  initial,
  onClose,
  onSaved,
}: {
  initial?: Quiz;
  onClose: () => void;
  onSaved: (quiz: Quiz) => void;
}) {
  const t = useT();
  const [title, setTitle] = useState(initial?.title ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [questions, setQuestions] = useState(initial?.questions ?? [blank()]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  function change(index: number, patch: Partial<QuizQuestion>) {
    setQuestions((q) =>
      q.map((item, i) => (i === index ? { ...item, ...patch } : item)),
    );
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t(initial ? "quiz.edit" : "quiz.create")}</DialogTitle>
        </DialogHeader>
        <label>
          {t("quiz.title")}
          <input
            dir="auto"
            className={fieldClass}
            value={title}
            maxLength={200}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          {t("quiz.description")}
          <textarea
            dir="auto"
            className={fieldClass}
            value={description}
            maxLength={2000}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        {questions.map((q, i) => (
          <fieldset
            key={q.id}
            className="border border-border p-4 rounded-xl space-y-3"
          >
            <legend className="px-2">
              {t("quiz.question")} {i + 1}
            </legend>
            <label>
              {t("quiz.type")}
              <select
                className={fieldClass}
                value={q.type}
                onChange={(e) =>
                  change(i, {
                    type: e.target.value as QuizQuestion["type"],
                    options:
                      e.target.value === "multiple_choice" ? ["", ""] : [],
                    correct_option:
                      e.target.value === "multiple_choice" ? 0 : null,
                  })
                }
              >
                <option value="short_answer">{t("quiz.shortAnswer")}</option>
                <option value="multiple_choice">
                  {t("quiz.multipleChoice")}
                </option>
              </select>
            </label>
            <label className="block">
              {t("quiz.question")}
              <textarea
                dir="auto"
                className={fieldClass}
                value={q.prompt}
                maxLength={2000}
                onChange={(e) => change(i, { prompt: e.target.value })}
              />
            </label>
            {q.type === "multiple_choice" ? (
              <>
                <label className="block">
                  {t("quiz.options")}
                  <textarea
                    dir="auto"
                    className={fieldClass}
                    value={q.options.join("\n")}
                    onChange={(e) =>
                      change(i, { options: e.target.value.split("\n") })
                    }
                  />
                </label>
                <label className="block">
                  {t("quiz.correctOption")}
                  <input
                    type="number"
                    min={1}
                    max={q.options.length}
                    className={fieldClass}
                    value={(q.correct_option ?? 0) + 1}
                    onChange={(e) =>
                      change(i, { correct_option: Number(e.target.value) - 1 })
                    }
                  />
                </label>
              </>
            ) : (
              <label className="block">
                {t("quiz.expected")}
                <textarea
                  dir="auto"
                  className={fieldClass}
                  value={q.answer ?? ""}
                  maxLength={2000}
                  onChange={(e) => change(i, { answer: e.target.value })}
                />
              </label>
            )}
            <label className="block">
              {t("quiz.explanation")}
              <textarea
                dir="auto"
                className={fieldClass}
                value={q.explanation ?? ""}
                maxLength={2000}
                onChange={(e) => change(i, { explanation: e.target.value })}
              />
            </label>
            <Button
              variant="ghost"
              disabled={questions.length === 1}
              onClick={() =>
                setQuestions((previous) => previous.filter((_, j) => j !== i))
              }
            >
              {t("quiz.removeQuestion")}
            </Button>
          </fieldset>
        ))}
        <Button
          variant="outline"
          disabled={questions.length >= 20}
          onClick={() => setQuestions((q) => [...q, blank()])}
        >
          <Plus className="size-4" />
          {t("quiz.addQuestion")}
        </Button>
        {error && (
          <p role="alert" className="text-destructive">
            {error}
          </p>
        )}
        <Button
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              onSaved(
                await quizzesApi.save(
                  {
                    title,
                    description,
                    questions: questions.map(({ id: _id, ...q }) => q),
                    expected_revision: initial?.revision,
                    source_conversation_id: initial?.source_conversation_id,
                    source_note_id: initial?.source_note_id,
                  },
                  initial?.id,
                ),
              );
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          {t("learn.save")}
        </Button>
      </DialogContent>
    </Dialog>
  );
}

export function QuizzesList({ recent = false }: { recent?: boolean }) {
  const t = useT();
  const navigate = useNavigate();
  const [quizzes, setQuizzes] = useState<Quiz[]>([]);
  const [search, setSearch] = useState("");
  const [create, setCreate] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const timer = setTimeout(() => {
      quizzesApi
        .list(search)
        .then((rows) => {
          if (active) setQuizzes(rows);
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    }, 200);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [search]);
  return (
    <section className="space-y-5">
      {recent && (
        <div className="flex items-center justify-between gap-3">
          <h2 className="font-medium">{t("quiz.recent")}</h2>
          <Button onClick={() => setCreate(true)}>
            <Plus className="size-4" />
            {t("quiz.create")}
          </Button>
        </div>
      )}
      {!recent && (
        <input
          className={fieldClass}
          aria-label={t("quiz.search")}
          placeholder={t("quiz.search")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      )}
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {!quizzes.length && (
        <p className="text-muted-foreground text-sm">{t("quiz.empty")}</p>
      )}
      <div className="grid md:grid-cols-2 gap-4">
        {(recent ? quizzes.slice(0, 4) : quizzes).map((quiz) => (
          <div
            key={quiz.id}
            className="border border-border/60 bg-card rounded-xl p-5 space-y-3"
          >
            <Link
              to={`/learn/quizzes/${quiz.id}`}
              className="font-medium hover:text-primary"
              dir="auto"
            >
              {quiz.title}
            </Link>
            <p dir="auto" className="text-sm text-muted-foreground">
              {quiz.description}
            </p>
            <div className="flex justify-between items-center">
              <span className="text-xs text-muted-foreground">
                {quiz.question_count} {t("quiz.questions")}
              </span>
              <Button
                size="sm"
                onClick={() => navigate(`/learn/quizzes/${quiz.id}/take`)}
              >
                {t("quiz.take")}
              </Button>
            </div>
          </div>
        ))}
      </div>
      {create && (
        <QuizEditor
          onClose={() => setCreate(false)}
          onSaved={(quiz) => navigate(`/learn/quizzes/${quiz.id}`)}
        />
      )}
    </section>
  );
}

export function QuizzesPage() {
  const { quizId } = useParams();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const t = useT();
  const [quiz, setQuiz] = useState<Quiz | null>(null);
  const [editor, setEditor] = useState<Quiz | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [history, setHistory] = useState<QuizAttempt[]>([]);
  const [result, setResult] = useState<QuizAttempt | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setQuiz(null);
    setResult(null);
    if (!quizId) return;
    let active = true;
    Promise.all([quizzesApi.get(quizId), quizzesApi.history(quizId)])
      .then(([q, h]) => {
        if (active) {
          setQuiz(q);
          setHistory(h);
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [quizId, pathname]);
  if (quizId && (pathname.endsWith("/take") || result))
    return (
      <QuizSession
        key={result?.id ?? quizId}
        quizId={quizId}
        initial={result ?? undefined}
        onClose={() => {
          setResult(null);
          navigate(`/learn/quizzes/${quizId}`);
        }}
      />
    );
  return (
    <PageContainer>
      <PageHeader
        title={quiz?.title ?? t("learn.quizzes")}
        description={!quizId ? t("quiz.subtitle") : undefined}
        actions={
          !quizId ? (
            <Button onClick={() => setCreating(true)}>
              <Plus className="size-4" />
              {t("quiz.create")}
            </Button>
          ) : undefined
        }
      />
      <LearnNav active="quizzes" />
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {!quizId ? (
        <QuizzesList />
      ) : (
        quiz && (
          <div className="space-y-6">
            <p dir="auto" className="text-muted-foreground">
              {quiz.description}
            </p>
            <p>
              {quiz.question_count} {t("quiz.questions")}
            </p>
            <SourceAction conversationId={quiz.source_conversation_id} />
            {quiz.source_note_id && (
              <Link
                className="text-sm text-primary"
                to={`/learn/notes/${quiz.source_note_id}`}
              >
                {t("learn.viewSource")}
              </Link>
            )}
            <p className="text-sm text-muted-foreground">
              {t(quiz.grounded ? "quiz.grounded" : "quiz.general")}
            </p>
            <div className="flex gap-3">
              <Button
                onClick={() => navigate(`/learn/quizzes/${quiz.id}/take`)}
              >
                {t("quiz.take")}
              </Button>
              <Button
                variant="outline"
                onClick={() =>
                  quizzesApi
                    .editor(quiz.id)
                    .then(setEditor)
                    .catch((e) => setError(e.message))
                }
              >
                <Pencil className="size-4" />
                {t("quiz.edit")}
              </Button>
              <Button variant="ghost" onClick={() => setDeleting(true)}>
                <Trash2 className="size-4" />
                {t("quiz.delete")}
              </Button>
            </div>
            <h2 className="font-medium">{t("quiz.history")}</h2>
            {history.map((a) => (
              <button
                key={a.id}
                className="block w-full text-start border border-border p-4 rounded-xl"
                onClick={() =>
                  a.submitted_at
                    ? setResult(a)
                    : navigate(`/learn/quizzes/${quiz.id}/take`)
                }
              >
                {new Date(a.created_at).toLocaleString()} ·{" "}
                {a.submitted_at
                  ? `${a.score} / ${a.snapshot.questions.length}`
                  : t("quiz.resume")}
              </button>
            ))}
          </div>
        )
      )}
      {creating && (
        <QuizEditor
          onClose={() => setCreating(false)}
          onSaved={(q) => {
            setCreating(false);
            navigate(`/learn/quizzes/${q.id}`);
          }}
        />
      )}
      {editor && (
        <QuizEditor
          initial={editor}
          onClose={() => setEditor(null)}
          onSaved={(q) => {
            setQuiz(q);
            setEditor(null);
          }}
        />
      )}
      {deleting && quiz && (
        <ConfirmDelete
          warning={t("quiz.delete")}
          onClose={() => setDeleting(false)}
          onConfirm={async () => {
            await quizzesApi.delete(quiz.id);
            navigate("/learn/quizzes");
          }}
        />
      )}
    </PageContainer>
  );
}
