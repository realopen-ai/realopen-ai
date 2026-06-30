import { Package } from "lucide-react";

export function AssetsSection() {
  return (
    <div className="flex flex-col items-center justify-center h-full p-8">
      <div className="w-16 h-16 rounded-2xl bg-purple-500/10 flex items-center justify-center mb-4">
        <Package className="w-8 h-8 text-purple-400" />
      </div>
      <h2 className="text-[15px] font-medium text-foreground mb-1">Assets</h2>
      <p className="text-[13px] text-muted-foreground/60 text-center max-w-sm">
        This section will contain reusable resources such as logos, company
        branding, custom icons, reusable images, voice assets, and other
        user-provided resources.
      </p>
      <p className="text-[11px] text-muted-foreground/40 mt-4">Coming soon</p>
    </div>
  );
}
