import { useState } from "react";
import { Loader2 } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { useT } from "@/store/settingsStore";
import { flashcardsApi, type CardInput } from "@/api/flashcardsClient";
import { CardMarkdown } from "./CardMarkdown";

export const fieldClass =
  "w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring/50";

export function CardEditor({
  initial,
  onSave,
  onClose,
  onReplace,
}: {
  initial?: CardInput;
  onSave: (card: CardInput) => Promise<void>;
  onClose: () => void;
  onReplace?: (cards: CardInput[]) => Promise<void>;
}) {
  const t = useT();
  const [front, setFront] = useState(initial?.front ?? "");
  const [back, setBack] = useState(initial?.back ?? "");
  const [reference, setReference] = useState(initial?.source_reference ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [proposal, setProposal] = useState<CardInput[] | null>(null);
  const [proposing, setProposing] = useState<string | null>(null);
  const input = (): CardInput => ({
    front,
    back,
    source_reference: reference || null,
    source_page: initial?.source_page,
    source_chunk_id: initial?.source_chunk_id,
    source_artifact: initial?.source_artifact,
  });
  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await onSave(input());
      onClose();
    } catch (error) {
      setError(error instanceof Error ? error.message : t("learn.error"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {t(initial ? "learn.editCard" : "learn.addCard")}
          </DialogTitle>
        </DialogHeader>
        <p className="text-xs text-muted-foreground">{t("learn.markdown")}</p>
        {initial && onReplace && (
          <div className="flex flex-wrap gap-2">
            {(["correct", "shorter", "harder", "recall", "split"] as const).map(
              (action) => (
                <Button
                  key={action}
                  size="sm"
                  variant="outline"
                  disabled={busy || !front.trim() || !back.trim()}
                  onClick={async () => {
                    setBusy(true);
                    setProposing(action);
                    setError("");
                    setProposal(null);
                    try {
                      setProposal(
                        (await flashcardsApi.rewrite(input(), action)).cards,
                      );
                    } catch (error) {
                      setError(
                        error instanceof Error
                          ? error.message
                          : t("learn.error"),
                      );
                    } finally {
                      setBusy(false);
                      setProposing(null);
                    }
                  }}
                >
                  {proposing === action && (
                    <Loader2 className="size-3 animate-spin" />
                  )}
                  {t(`learn.${action}`)}
                </Button>
              ),
            )}
          </div>
        )}
        {proposal && (
          <section className="rounded-lg border border-primary/30 p-3 space-y-3 max-h-64 overflow-y-auto">
            <p className="text-xs text-muted-foreground">
              {t("learn.proposalNotice")}
            </p>
            {proposal.map((card, index) => (
              <div key={index} className="space-y-2">
                <CardMarkdown text={card.front} />
                <CardMarkdown text={card.back} />
              </div>
            ))}
            <div className="flex gap-2">
              <Button
                size="sm"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError("");
                  try {
                    await onReplace?.(proposal);
                    onClose();
                  } catch (error) {
                    setError(
                      error instanceof Error ? error.message : t("learn.error"),
                    );
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {t("learn.acceptProposal")}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={busy}
                onClick={() => setProposal(null)}
              >
                {t("learn.discard")}
              </Button>
            </div>
          </section>
        )}
        <label className="text-sm space-y-1">
          {t("learn.front")}
          <textarea
            dir="auto"
            maxLength={2000}
            rows={4}
            className={fieldClass}
            value={front}
            onChange={(e) => setFront(e.target.value)}
          />
        </label>
        <label className="text-sm space-y-1">
          {t("learn.back")}
          <textarea
            dir="auto"
            maxLength={4000}
            rows={5}
            className={fieldClass}
            value={back}
            onChange={(e) => setBack(e.target.value)}
          />
        </label>
        <label className="text-sm space-y-1">
          {t("learn.reference")}
          <input
            maxLength={500}
            className={fieldClass}
            value={reference}
            onChange={(e) => setReference(e.target.value)}
          />
        </label>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            {t("learn.cancel")}
          </Button>
          <Button
            disabled={busy || !front.trim() || !back.trim()}
            onClick={() => void save()}
          >
            {t(
              proposing
                ? "learn.proposing"
                : busy
                  ? "learn.saving"
                  : "learn.save",
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ConfirmDelete({
  onConfirm,
  onClose,
  warning,
}: {
  onConfirm: () => Promise<void>;
  onClose: () => void;
  warning?: string;
}) {
  const t = useT();
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
          <DialogTitle>{t("learn.deleteConfirm")}</DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          {warning ?? t("learn.deleteWarning")}
        </p>
        {error && <p role="alert">{error}</p>}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            {t("learn.cancel")}
          </Button>
          <Button
            variant="destructive"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await onConfirm();
                onClose();
              } catch {
                setError(t("learn.error"));
              } finally {
                setBusy(false);
              }
            }}
          >
            {t("learn.delete")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
