import { useEffect, useState } from "react";
import { Cpu, Database, MemoryStick } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useSandboxStore, type Sandbox } from "@/store/sandboxStore";
import {
  sandboxResourcePayload,
  sandboxResourcesAreValid,
} from "@/store/sandboxResources";
import { useT } from "@/store/settingsStore";

const inputClass =
  "h-9 w-full rounded-lg border border-border/60 bg-transparent px-3 text-[13.5px] text-foreground outline-none transition-colors focus:border-primary/50 placeholder:text-muted-foreground/70";

function ResourceField({
  icon: Icon,
  label,
  hint,
  value,
  onChange,
  min,
  max,
  step,
  suffix,
}: {
  icon: typeof Cpu;
  label: string;
  hint: string;
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step: number;
  suffix: string;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-2 text-xs font-medium text-foreground">
        <Icon className="h-3.5 w-3.5 text-muted-foreground" />
        {label}
      </div>
      <div className="flex items-center gap-2">
        <input
          type="number"
          value={value}
          min={min}
          max={max}
          step={step}
          onChange={(event) => onChange(Number(event.target.value))}
          className="h-9 min-w-0 flex-1 rounded-lg border border-border/60 bg-transparent px-2.5 text-[13.5px] text-foreground outline-none transition-colors focus:border-primary/50"
        />
        <span className="w-10 shrink-0 text-xs text-muted-foreground">
          {suffix}
        </span>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">{hint}</p>
    </div>
  );
}

export function CreateSandboxDialog({
  open,
  onOpenChange,
  conversationId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  conversationId?: string | null;
}) {
  const t = useT();
  const createSandbox = useSandboxStore((state) => state.createSandbox);
  const [name, setName] = useState("New workspace");
  const [cpu, setCpu] = useState(2);
  const [memoryGb, setMemoryGb] = useState(2);
  const [storageGb, setStorageGb] = useState(2);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (open) setError("");
  }, [open]);

  const submit = async () => {
    if (!name.trim() || !sandboxResourcesAreValid(cpu, memoryGb, storageGb))
      return;
    setSubmitting(true);
    setError("");
    try {
      const resources = sandboxResourcePayload(memoryGb, storageGb);
      await createSandbox({
        name: name.trim(),
        conversationId,
        cpuLimit: cpu,
        ...resources,
      });
      onOpenChange(false);
      setName("New workspace");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="text-[16px] font-semibold">
            {t("workspace.createSandbox")}
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-relaxed">
            {t("workspace.createDescription")}
          </DialogDescription>
        </DialogHeader>

        <label className="mt-5 block">
          <span className="mb-1.5 block text-xs font-medium text-foreground">
            {t("workspace.name")}
          </span>
          <input
            autoFocus
            value={name}
            maxLength={120}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && void submit()}
            className={inputClass}
          />
        </label>

        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <ResourceField
            icon={Cpu}
            label="CPU"
            hint="0.25–8 cores"
            value={cpu}
            onChange={setCpu}
            min={0.25}
            max={8}
            step={0.25}
            suffix="cores"
          />
          <ResourceField
            icon={MemoryStick}
            label="RAM"
            hint="0.25–16 GB"
            value={memoryGb}
            onChange={setMemoryGb}
            min={0.25}
            max={16}
            step={0.25}
            suffix="GB"
          />
          <ResourceField
            icon={Database}
            label={t("workspace.storage")}
            hint="0.0625–50 GB"
            value={storageGb}
            onChange={setStorageGb}
            min={0.0625}
            max={50}
            step={0.25}
            suffix="GB"
          />
        </div>

        {error && <p className="mt-3 text-xs text-danger">{error}</p>}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost" disabled={submitting}>
              {t("workspace.cancel")}
            </Button>
          </DialogClose>
          <Button
            onClick={() => void submit()}
            disabled={
              submitting ||
              !name.trim() ||
              !sandboxResourcesAreValid(cpu, memoryGb, storageGb)
            }
          >
            {submitting ? t("workspace.creating") : t("workspace.createSandbox")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function DeleteSandboxDialog({
  sandbox,
  onOpenChange,
}: {
  sandbox: Sandbox | null;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const deleteSandbox = useSandboxStore((state) => state.deleteSandbox);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");
  const remove = async () => {
    if (!sandbox) return;
    setDeleting(true);
    setError("");
    try {
      await deleteSandbox(sandbox.id);
      onOpenChange(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setDeleting(false);
    }
  };
  return (
    <Dialog open={Boolean(sandbox)} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="text-[16px] font-semibold">
            {t("workspace.deleteTitle")}
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-relaxed">
            <strong className="text-foreground">{sandbox?.name}</strong>{" "}
            {t("workspace.deleteDescription")}
          </DialogDescription>
        </DialogHeader>
        {error && <p className="mt-3 text-xs text-danger">{error}</p>}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost" disabled={deleting}>
              {t("workspace.cancel")}
            </Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={() => void remove()}
            disabled={deleting}
          >
            {deleting ? t("workspace.deleting") : t("workspace.deleteSandbox")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
