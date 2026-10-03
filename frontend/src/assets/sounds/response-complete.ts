type WebkitWindow = typeof window & {
  webkitAudioContext?: typeof AudioContext;
};

let audioContext: AudioContext | null = null;

function context(): AudioContext | null {
  try {
    const AudioContextClass =
      window.AudioContext ??
      (window as WebkitWindow).webkitAudioContext;
    if (!AudioContextClass) return null;
    audioContext ??= new AudioContextClass();
    return audioContext;
  } catch {
    return null;
  }
}

function unlock() {
  const current = context();
  if (current?.state === "suspended") void current.resume().catch(() => {});
}

if (typeof window !== "undefined") {
  // Browsers permit audio after a normal user gesture. Keep that unlocked
  // context for later completions, which normally happen asynchronously.
  window.addEventListener("pointerdown", unlock, { passive: true });
  window.addEventListener("keydown", unlock, { passive: true });
  window.addEventListener("touchstart", unlock, { passive: true });
}

function ring(current: AudioContext) {
  try {
    const gain = current.createGain();
    const oscillator = current.createOscillator();
    const start = current.currentTime;
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(660, start);
    oscillator.frequency.exponentialRampToValueAtTime(880, start + 0.12);
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.06, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.2);
    oscillator.connect(gain).connect(current.destination);
    oscillator.start(start);
    oscillator.stop(start + 0.21);
  } catch {
    // Autoplay and audio-device failures are intentionally non-fatal.
  }
}

/** Offline, dependency-free UI chime. Kept with frontend sound assets so the
 * completion manager only decides *when* to play it. */
export function playResponseCompleteSound() {
  const current = context();
  if (!current) return;
  if (current.state === "running") {
    ring(current);
    return;
  }
  void current
    .resume()
    .then(() => {
      if (current.state === "running") ring(current);
    })
    .catch(() => {});
}
