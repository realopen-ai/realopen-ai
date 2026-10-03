/** Offline, dependency-free UI chime. Kept with frontend sound assets so the
 * completion manager only decides *when* to play it. */
export function playResponseCompleteSound() {
  try {
    const AudioContextClass =
      window.AudioContext ??
      (window as typeof window & { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!AudioContextClass) return;
    const context = new AudioContextClass();
    if (context.state !== "running") {
      void context.close();
      return;
    }
    const gain = context.createGain();
    const oscillator = context.createOscillator();
    const start = context.currentTime;
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(660, start);
    oscillator.frequency.exponentialRampToValueAtTime(880, start + 0.12);
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.06, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.2);
    oscillator.connect(gain).connect(context.destination);
    oscillator.start(start);
    oscillator.stop(start + 0.21);
    oscillator.addEventListener("ended", () => void context.close());
  } catch {
    // Autoplay and audio-device failures are intentionally non-fatal.
  }
}
