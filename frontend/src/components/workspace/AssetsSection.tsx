import { Package } from "lucide-react";
import { EmptyState } from "@/components/ui/primitives";
import { useT } from "@/store/settingsStore";

export function AssetsSection() {
  const t = useT();
  return (
    <div className="flex h-full flex-col items-center justify-center p-8">
      <EmptyState
        icon={<Package />}
        title={t("workspace.assets")}
        description={t("workspace.assets.emptyDescription")}
        action={
          <span className="inline-flex items-center rounded-full bg-secondary px-2.5 py-1 text-xs font-medium text-muted-foreground">
            {t("workspace.comingSoon")}
          </span>
        }
        className="rounded-xl"
      />
    </div>
  );
}
