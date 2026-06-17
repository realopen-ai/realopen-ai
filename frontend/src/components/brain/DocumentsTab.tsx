import { FileText } from "lucide-react";
import { useT } from "@/store/settingsStore";

export function DocumentsTab() {
  const t = useT();

  return (
    <div className="flex flex-col items-center justify-center h-full py-16 text-center">
      <FileText className="w-10 h-10 text-muted-foreground/20 mb-3" />
      <p className="text-[14px] font-medium text-muted-foreground/60 mb-1">
        {t("brain.documents.comingSoon")}
      </p>
      <p className="text-[12px] text-muted-foreground/40 max-w-[260px]">
        {t("brain.documents.description")}
      </p>
    </div>
  );
}
