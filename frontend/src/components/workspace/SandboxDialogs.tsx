import { useEffect, useState } from "react";
import { Box, Cpu, Database, MemoryStick, Trash2 } from "lucide-react";
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
import { sandboxResourcePayload, sandboxResourcesAreValid } from "@/store/sandboxResources";

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
    <label className="rounded-xl border border-border/70 bg-secondary/20 p-3">
      <span className="mb-2 flex items-center gap-2 text-xs font-medium">
        <Icon className="h-3.5 w-3.5 text-primary" />
        {label}
      </span>
      <div className="flex items-center gap-2">
        <input
          type="number"
          value={value}
          min={min}
          max={max}
          step={step}
          onChange={(event) => onChange(Number(event.target.value))}
          className="h-9 min-w-0 flex-1 rounded-lg border border-border bg-background px-2.5 text-sm outline-none focus:border-primary"
        />
        <span className="w-9 text-[11px] text-muted-foreground">{suffix}</span>
      </div>
      <span className="mt-1.5 block text-[10px] text-muted-foreground">{hint}</span>
    </label>
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
    if (!name.trim() || !sandboxResourcesAreValid(cpu, memoryGb, storageGb)) return;
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
          <DialogTitle className="flex items-center gap-2 text-base font-semibold">
            <Box className="h-4 w-4 text-primary" /> Create sandbox
          </DialogTitle>
          <DialogDescription className="text-xs text-muted-foreground">
            Configure the isolated workspace resources. You can keep working files across restarts.
          </DialogDescription>
        </DialogHeader>

        <label className="mt-5 block">
          <span className="mb-1.5 block text-xs font-medium">Name</span>
          <input
            autoFocus
            value={name}
            maxLength={120}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && void submit()}
            className="h-10 w-full rounded-lg border border-border bg-background px-3 text-sm outline-none focus:border-primary"
          />
        </label>

        <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-3">
          <ResourceField icon={Cpu} label="CPU" hint="0.25–8 cores" value={cpu} onChange={setCpu} min={0.25} max={8} step={0.25} suffix="cores" />
          <ResourceField icon={MemoryStick} label="RAM" hint="0.25–16 GB" value={memoryGb} onChange={setMemoryGb} min={0.25} max={16} step={0.25} suffix="GB" />
          <ResourceField icon={Database} label="Storage" hint="0.0625–50 GB" value={storageGb} onChange={setStorageGb} min={0.0625} max={50} step={0.25} suffix="GB" />
        </div>

        {error && <p className="mt-3 text-xs text-destructive">{error}</p>}
        <DialogFooter>
          <DialogClose asChild><Button variant="outline" disabled={submitting}>Cancel</Button></DialogClose>
          <Button onClick={() => void submit()} disabled={submitting || !name.trim() || !sandboxResourcesAreValid(cpu, memoryGb, storageGb)}>{submitting ? "Creating…" : "Create sandbox"}</Button>
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
          <DialogTitle className="flex items-center gap-2 text-base font-semibold"><Trash2 className="h-4 w-4 text-destructive" /> Delete sandbox?</DialogTitle>
          <DialogDescription className="text-xs leading-relaxed text-muted-foreground">
            <strong className="text-foreground">{sandbox?.name}</strong> and every file in its persistent workspace will be permanently deleted. This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        {error && <p className="mt-3 text-xs text-destructive">{error}</p>}
        <DialogFooter>
          <DialogClose asChild><Button variant="outline" disabled={deleting}>Cancel</Button></DialogClose>
          <Button className="bg-destructive text-destructive-foreground hover:bg-destructive/90" onClick={() => void remove()} disabled={deleting}>{deleting ? "Deleting…" : "Delete sandbox"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
