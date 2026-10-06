import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { useT } from "@/store/settingsStore";
import type { CardInput } from "@/api/flashcardsClient";

export const fieldClass =
  "w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring/50";

export function CardEditor({
  initial,
  onSave,
  onClose,
}: {
  initial?: CardInput;
  onSave: (card: CardInput) => Promise<void>;
  onClose: () => void;
}) {
  const t = useT();
  const [front, setFront] = useState(initial?.front ?? "");
  const [back, setBack] = useState(initial?.back ?? "");
  const [reference, setReference] = useState(initial?.source_reference ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await onSave({ front, back, source_reference: reference || null });
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
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {t(initial ? "learn.editCard" : "learn.addCard")}
          </DialogTitle>
        </DialogHeader>
        <p className="text-xs text-muted-foreground">{t("learn.markdown")}</p>
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
            {t(busy ? "learn.saving" : "learn.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ConfirmDelete({
  onConfirm,
  onClose,
}: {
  onConfirm: () => Promise<void>;
  onClose: () => void;
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
          {t("learn.deleteWarning")}
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
