import { Layers, Play } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { useT } from "@/store/settingsStore";
import { useLearnStore } from "@/store/learnStore";
import { useUIStore } from "@/store/uiStore";

export function DeckArtifact({
  id,
  title,
  count,
}: {
  id: string;
  title: string;
  count: number;
}) {
  const t = useT();
  const navigate = useNavigate();
  const study = () => {
    if (window.matchMedia("(min-width: 768px)").matches) {
      useLearnStore.getState().setStudyDeck(id);
      useUIStore.getState().setRightPanelOpen(true);
    } else navigate(`/learn/flashcards/${id}/study`);
  };
  return (
    <div className="my-3 max-w-md rounded-xl border border-border/60 bg-card p-4">
      <div className="flex gap-3 items-center">
        <Layers className="size-5 text-primary shrink-0" />
        <div className="min-w-0">
          <p className="font-medium truncate">{title}</p>
          <p className="text-xs text-muted-foreground">
            {t("learn.cardCount", { count })}
          </p>
        </div>
      </div>
      <div className="flex gap-2 mt-3">
        <Button size="sm" onClick={study}>
          <Play className="size-3.5" />
          {t("learn.study")}
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => navigate(`/learn/flashcards/${id}`)}
        >
          {t("learn.open")}
        </Button>
      </div>
    </div>
  );
}
