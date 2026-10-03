const SAMPLE_RATE = 16_000;
const DURATION_SECONDS = 0.24;

function writeAscii(view: DataView, offset: number, value: string) {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index));
  }
}

/** Build a tiny two-tone PCM WAV locally. This remains a bundled offline
 * sound asset without adding a large binary to the application. */
function notificationWav(): Blob {
  const sampleCount = Math.floor(SAMPLE_RATE * DURATION_SECONDS);
  const bytes = new ArrayBuffer(44 + sampleCount * 2);
  const view = new DataView(bytes);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + sampleCount * 2, true);
  writeAscii(view, 8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, SAMPLE_RATE, true);
  view.setUint32(28, SAMPLE_RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, sampleCount * 2, true);

  for (let index = 0; index < sampleCount; index += 1) {
    const time = index / SAMPLE_RATE;
    const frequency = time < 0.11 ? 660 : 880;
    const attack = Math.min(1, time / 0.018);
    const release = Math.min(1, (DURATION_SECONDS - time) / 0.07);
    const envelope = Math.max(0, Math.min(attack, release));
    const sample = Math.sin(2 * Math.PI * frequency * time) * envelope * 0.22;
    view.setInt16(44 + index * 2, Math.round(sample * 32767), true);
  }
  return new Blob([bytes], { type: "audio/wav" });
}

let player: HTMLAudioElement | null = null;
let unlocked = false;

function getPlayer(): HTMLAudioElement | null {
  if (typeof Audio === "undefined") return null;
  if (!player) {
    player = new Audio(URL.createObjectURL(notificationWav()));
    player.preload = "auto";
    player.volume = 0.45;
  }
  return player;
}

function unlock() {
  if (unlocked) return;
  const audio = getPlayer();
  if (!audio) return;
  const intendedVolume = audio.volume;
  audio.volume = 0;
  void audio
    .play()
    .then(() => {
      audio.pause();
      audio.currentTime = 0;
      audio.volume = intendedVolume;
      unlocked = true;
    })
    .catch(() => {
      audio.volume = intendedVolume;
    });
}

if (typeof window !== "undefined") {
  window.addEventListener("pointerdown", unlock, { passive: true });
  window.addEventListener("keydown", unlock, { passive: true });
  window.addEventListener("touchstart", unlock, { passive: true });
}

export function playResponseCompleteSound() {
  const audio = getPlayer();
  if (!audio) return;
  audio.currentTime = 0;
  void audio.play().catch(() => {});
}
