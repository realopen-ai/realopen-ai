import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  Expand,
  Grip,
  Mic,
  MicOff,
  Minimize2,
  PhoneOff,
  Square,
  Volume2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { t } from "@/store/settingsStore";
import { useVoiceStore } from "@/voice/voiceStore";
import {
  nearestCallOverlayCorner,
  type CallOverlayCorner,
} from "@/voice/callOverlay";
import { cn } from "@/lib/utils";

interface VoiceCallPanelProps {
  onToggleMute: () => void;
  onEnd: () => void;
  onStopResponse: () => void;
}

const cornerClass: Record<CallOverlayCorner, string> = {
  "top-left": "left-4 top-4",
  "top-right": "right-4 top-4",
  "bottom-left": "bottom-4 left-4",
  "bottom-right": "bottom-4 right-4",
};

export function VoiceCallPanel({
  onToggleMute,
  onEnd,
  onStopResponse,
}: VoiceCallPanelProps) {
  const voiceState = useVoiceStore((s) => s.voiceState);
  const isMicMuted = useVoiceStore((s) => s.isMicMuted);
  const micLevel = useVoiceStore((s) => s.micLevel);
  const partialTranscript = useVoiceStore((s) => s.partialTranscript);
  const [minimized, setMinimized] = useState(false);
  const [corner, setCorner] = useState<CallOverlayCorner>("top-right");
  const [dragging, setDragging] = useState(false);
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 });
  const dragStartRef = useRef({ x: 0, y: 0 });
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (voiceState === "connecting") setMinimized(false);
  }, [voiceState]);

  if (voiceState === "inactive") return null;

  const isSpeaking = voiceState === "speaking";
  const canStopResponse =
    voiceState === "processing" ||
    voiceState === "speaking" ||
    voiceState === "interrupting";
  const stateLabel = isMicMuted
    ? t("voice.state.muted")
    : t(`voice.state.${voiceState === "interrupting" ? "interrupted" : voiceState}`);

  const beginDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!minimized || (event.target as HTMLElement).closest("button")) return;
    dragStartRef.current = { x: event.clientX, y: event.clientY };
    setDragOffset({ x: 0, y: 0 });
    setDragging(true);
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const previewDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    setDragOffset({
      x: event.clientX - dragStartRef.current.x,
      y: event.clientY - dragStartRef.current.y,
    });
  };

  const finishDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!minimized || !dragging) return;
    const bounds = panelRef.current?.parentElement?.getBoundingClientRect();
    if (bounds) {
      setCorner(
        nearestCallOverlayCorner(
          { x: event.clientX, y: event.clientY },
          bounds,
        ),
      );
    }
    setDragging(false);
    setDragOffset({ x: 0, y: 0 });
  };

  return (
    <div
      ref={panelRef}
      className={cn(
        "voice-call-panel z-40 overflow-hidden border-border bg-card/95 shadow-2xl backdrop-blur-xl",
        voiceState === "stopping" && "voice-call-panel-exit",
        dragging && "voice-call-dragging",
        minimized
          ? `absolute h-48 w-72 rounded-3xl border ${cornerClass[corner]}`
          : "relative h-[40%] min-h-60 shrink-0 border-b",
      )}
      style={
        minimized
          ? {
              transform: `translate3d(${dragOffset.x}px, ${dragOffset.y}px, 0) scale(${dragging ? 1.035 : 1})`,
            }
          : undefined
      }
      aria-label="Voice call"
    >
      {dragging && (
        <div className="voice-drag-bubbles" aria-hidden="true">
          <span />
          <span />
          <span />
          <span />
        </div>
      )}
      <div
        className={cn(
          "absolute inset-x-0 top-0 z-10 flex h-12 items-center justify-between px-4",
          minimized && (dragging ? "cursor-grabbing" : "cursor-grab"),
        )}
        onPointerDown={beginDrag}
        onPointerMove={previewDrag}
        onPointerUp={finishDrag}
        onPointerCancel={finishDrag}
      >
        <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          {minimized && <Grip className="h-3.5 w-3.5" />}
          <span className="h-2 w-2 rounded-full bg-primary animate-pulse" />
          {stateLabel}
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 rounded-full"
          onClick={() => setMinimized((value) => !value)}
          aria-label={minimized ? t("voice.call.expand") : t("voice.call.minimize")}
          title={minimized ? t("voice.call.expand") : t("voice.call.minimize")}
        >
          {minimized ? <Expand className="h-4 w-4" /> : <Minimize2 className="h-4 w-4" />}
        </Button>
      </div>

      <div className="flex h-full flex-col items-center justify-center px-6 pb-14 pt-11">
        <div className={cn("voice-agent-orb", isSpeaking && "is-speaking", voiceState === "connecting" && "is-connecting")}>
          <div className="voice-agent-orb-core">
            {isSpeaking ? <Volume2 className="h-7 w-7" /> : <span className="text-lg font-semibold">AI</span>}
          </div>
        </div>

        {!minimized && (
          <div className="mt-5 flex h-9 items-center justify-center gap-1" aria-label="Microphone level">
            {[0.65, 1, 0.8, 1.2, 0.7].map((weight, index) => (
              <span
                key={index}
                className="voice-user-level-bar"
                style={{
                  height: `${Math.max(5, Math.min(32, micLevel * 34 * weight))}px`,
                  opacity: isMicMuted ? 0.2 : 0.45 + micLevel * 0.55,
                }}
              />
            ))}
          </div>
        )}

        {!minimized && partialTranscript && !isMicMuted && (
          <p className="mt-2 max-w-xl truncate text-center text-xs text-muted-foreground" aria-live="polite">
            “{partialTranscript}”
          </p>
        )}
      </div>

      <div className="absolute inset-x-0 bottom-4 flex items-center justify-center gap-3">
        <Button
          variant="secondary"
          size="icon"
          className={cn("h-10 w-10 rounded-full", isMicMuted && "text-amber-500")}
          onClick={onToggleMute}
          aria-label={isMicMuted ? t("voice.button.unmute") : t("voice.button.mute")}
          title={isMicMuted ? t("voice.button.unmute") : t("voice.button.mute")}
        >
          {isMicMuted ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
        </Button>
        {canStopResponse && (
          <Button
            variant="secondary"
            size="icon"
            className="h-10 w-10 rounded-full"
            onClick={onStopResponse}
            aria-label={t("input.stopResponse")}
            title={t("input.stopResponse")}
          >
            <Square className="h-3.5 w-3.5 fill-current" />
          </Button>
        )}
        <Button
          size="icon"
          className="h-11 w-11 rounded-full bg-red-500 text-white hover:bg-red-600"
          onClick={onEnd}
          aria-label={t("voice.call.end")}
          title={t("voice.call.end")}
        >
          <PhoneOff className="h-4.5 w-4.5" />
        </Button>
      </div>
    </div>
  );
}
