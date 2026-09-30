import { Package } from "lucide-react";
import { EmptyState } from "@/components/ui/primitives";

export function AssetsSection() {
  return (
    <div className="flex h-full flex-col items-center justify-center p-8">
      <EmptyState
        icon={<Package />}
        title="Assets"
        description="This section will contain reusable resources such as logos, company branding, custom icons, reusable images, voice assets, and other user-provided resources."
        action={
          <span className="inline-flex items-center rounded-full bg-secondary px-2.5 py-1 text-xs font-medium text-muted-foreground">
            Coming soon
          </span>
        }
        className="rounded-xl"
      />
    </div>
  );
}
