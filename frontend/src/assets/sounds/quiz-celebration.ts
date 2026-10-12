let context: AudioContext | null = null;

/** Unlock during submission's user gesture so delayed grading can play sound. */
export function prepareQuizCelebrationSound() {
  if (typeof window === "undefined" || !window.AudioContext) return;
  try {
    context ??= new window.AudioContext();
    if (context.state === "suspended") void context.resume().catch(() => {});
  } catch {
    // Audio is optional; an unavailable device must not interrupt submission.
  }
}

/** A soft ascending major arpeggio, synthesized locally without network assets. */
export function playQuizCelebrationSound() {
  if (!context || context.state !== "running") return;
  const start = context.currentTime;
  const notes = [523.25, 659.25, 783.99, 1046.5];
  notes.forEach((frequency, index) => {
    const at = start + index * 0.11;
    const oscillator = context!.createOscillator();
    const gain = context!.createGain();
    oscillator.type = "sine";
    oscillator.frequency.value = frequency;
    gain.gain.setValueAtTime(0, at);
    gain.gain.linearRampToValueAtTime(0.12, at + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.001, at + 0.45);
    oscillator.connect(gain);
    gain.connect(context!.destination);
    oscillator.onended = () => {
      oscillator.disconnect();
      gain.disconnect();
    };
    oscillator.start(at);
    oscillator.stop(at + 0.46);
  });
}
