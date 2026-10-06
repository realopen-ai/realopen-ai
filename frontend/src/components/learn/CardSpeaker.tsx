import { useEffect, useRef, useState } from "react";
import { Volume2, Square, Loader2 } from "lucide-react";
import { ReadAloudPlayer } from "@/voice/readAloud";
import { Button } from "@/components/ui/button";
import { useT } from "@/store/settingsStore";

export function CardSpeaker({ text }: { text: string }) {
  const t = useT();
  const player = useRef<ReadAloudPlayer | null>(null);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => () => player.current?.stop(), [text]);
  return (
    <div className="flex items-center gap-2">
      <Button
        variant="ghost"
        size="icon"
        title={t(playing ? "learn.stopSpeech" : "learn.listen")}
        onClick={() => {
          if (playing) return player.current?.stop();
          player.current ??= new ReadAloudPlayer();
          setPlaying(true);
          setUnavailable(false);
          const spoken = text
            .replace(/```[^\n]*\n([\s\S]*?)```/g, "$1")
            .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
            .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
            .replace(/[*_`#]/g, "");
          void player.current.play(
            spoken,
            () => {
              setPlaying(false);
              setLoading(false);
            },
            (state) => setLoading(state.loading),
            { localOnly: true, onUnavailable: () => setUnavailable(true) },
          );
        }}
      >
        {loading ? (
          <Loader2 className="size-4 animate-spin" />
        ) : playing ? (
          <Square className="size-4" />
        ) : (
          <Volume2 className="size-4" />
        )}
      </Button>
      {unavailable && (
        <span role="status" className="text-xs text-muted-foreground">
          {t("learn.speechUnavailable")}
        </span>
      )}
    </div>
  );
}
