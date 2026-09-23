import { useEffect, useState } from "react";
import { Play, Plus, RotateCw, Square, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSandboxStore } from "@/store/sandboxStore";
import { CreateSandboxDialog, DeleteSandboxDialog } from "@/components/workspace/SandboxDialogs";

export function SandboxesSection() {
  const { sandboxes, loading, error, loadSandboxes, setSandboxId, lifecycle } = useSandboxStore();
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<(typeof sandboxes)[number] | null>(null);
  useEffect(() => { void loadSandboxes(); }, [loadSandboxes]);
  const act = async (id: string, action: "start"|"stop"|"restart") => { setSandboxId(id); await lifecycle(action); await loadSandboxes(); };
  return <div className="h-full overflow-y-auto p-6">
    <div className="mb-5 flex items-center justify-between gap-3">
      <div><h2 className="text-sm font-semibold">Sandboxes</h2><p className="text-xs text-muted-foreground">Persistent isolated coding environments</p></div>
      <Button onClick={() => setCreateOpen(true)}><Plus className="mr-1.5 h-3.5 w-3.5" />Create sandbox</Button>
    </div>
    {error && <p className="mb-3 text-sm text-destructive">{error}</p>}
    {loading ? <p className="text-sm text-muted-foreground">Loading…</p> : <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {sandboxes.map((item)=><div key={item.id} className="rounded-xl border border-border bg-card p-4">
        <div className="mb-3 flex items-start justify-between"><div><h3 className="font-medium">{item.name}</h3><p className="text-xs text-muted-foreground">{item.status} · {item.cpu_limit} CPU · {item.memory_limit_mb} MB</p></div><span className={`mt-1 h-2.5 w-2.5 rounded-full ${item.status==="running"?"bg-emerald-500":"bg-muted-foreground/40"}`} /></div>
        <div className="mb-3 h-1.5 overflow-hidden rounded-full bg-secondary"><div className="h-full bg-primary" style={{width:`${Math.min(100,(item.usage_bytes/item.workspace_quota_bytes)*100)}%`}} /></div>
        <p className="mb-3 text-[11px] text-muted-foreground">{(item.usage_bytes/1048576).toFixed(1)} MB of {(item.workspace_quota_bytes/1073741824).toFixed(1)} GB</p>
        <div className="flex gap-1"><Button variant="outline" size="sm" onClick={()=>void act(item.id,item.status==="running"?"stop":"start")}>{item.status==="running"?<Square className="h-3 w-3"/>:<Play className="h-3 w-3"/>}</Button><Button variant="outline" size="sm" onClick={()=>void act(item.id,"restart")}><RotateCw className="h-3 w-3"/></Button><Button variant="outline" size="sm" onClick={()=>setDeleteTarget(item)}><Trash2 className="h-3 w-3"/></Button></div>
      </div>)}
    </div>}
    <CreateSandboxDialog open={createOpen} onOpenChange={setCreateOpen} />
    <DeleteSandboxDialog sandbox={deleteTarget} onOpenChange={(open) => !open && setDeleteTarget(null)} />
  </div>;
}
