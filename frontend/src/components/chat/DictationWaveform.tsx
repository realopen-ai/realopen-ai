import { t } from "@/store/settingsStore";

export const WAVEFORM_BARS = 64;

/** Recent measured microphone energy, oldest left / newest right. */
export function DictationWaveform({
  levels,
  transcribing,
}: {
  levels: number[];
  transcribing: boolean;
}) {
  return (
    <div
      className="flex h-13 items-center justify-center px-4 pt-3.5 pb-1.5"
      role="status"
      aria-label={t(
        transcribing
          ? "input.dictation.transcribing"
          : "input.dictation.recording",
      )}
    >
      {transcribing ? (
        <span className="text-sm text-muted-foreground">
          {t("input.dictation.transcribing")}
        </span>
      ) : (
        <svg
          viewBox="0 0 384 72"
          className="h-8 w-full max-w-lg text-foreground"
          aria-hidden="true"
          preserveAspectRatio="none"
          style={{ direction: "ltr" }}
        >
          {levels.map((level, index) => {
            const height = 2 + level * 64;
            return (
              <line
                key={index}
                x1={index * 6 + 3}
                x2={index * 6 + 3}
                y1={(72 - height) / 2}
                y2={(72 + height) / 2}
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
            );
          })}
        </svg>
      )}
    </div>
  );
}
