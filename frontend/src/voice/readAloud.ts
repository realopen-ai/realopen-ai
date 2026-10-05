/** Configured voice first; browser speech is only a failure fallback. */
let activePlayer: ReadAloudPlayer | null = null;

export class ReadAloudPlayer {
  private controller: AbortController | null = null;
  private audio: HTMLAudioElement | null = null;
  private url: string | null = null;
  private utterance: SpeechSynthesisUtterance | null = null;
  private onDone: (() => void) | null = null;

  stop(): void {
    this.controller?.abort();
    this.controller = null;
    this.audio?.pause();
    if (this.audio) this.audio.src = "";
    this.audio = null;
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = null;
    if (this.utterance) window.speechSynthesis?.cancel();
    this.utterance = null;
    if (activePlayer === this) activePlayer = null;
    const onDone = this.onDone;
    this.onDone = null;
    onDone?.();
  }

  async play(text: string, onDone: () => void): Promise<void> {
    this.stop();
    activePlayer?.stop();
    activePlayer = this;
    this.onDone = onDone;
    const controller = new AbortController();
    this.controller = controller;
    const finish = () => {
      if (controller.signal.aborted) return;
      this.stop();
    };
    let fallingBack = false;
    const fallback = () => {
      if (controller.signal.aborted || fallingBack) return;
      fallingBack = true;
      this.audio?.pause();
      this.audio = null;
      if (this.url) URL.revokeObjectURL(this.url);
      this.url = null;
      if (!("speechSynthesis" in window)) return finish();
      const utterance = new SpeechSynthesisUtterance(text);
      this.utterance = utterance;
      utterance.onend = finish;
      utterance.onerror = finish;
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(utterance);
    };
    try {
      const response = await fetch("/api/voice/speech", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("Speech unavailable");
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      if (!blob.size) throw new Error("Empty speech audio");
      this.url = URL.createObjectURL(blob);
      const audio = new Audio(this.url);
      this.audio = audio;
      const speed = Number(response.headers.get("X-Playback-Speed"));
      audio.playbackRate = [0.5, 1, 1.5, 2].includes(speed) ? speed : 1;
      audio.onended = finish;
      audio.onerror = fallback;
      await audio.play();
    } catch {
      fallback();
    }
  }
}
