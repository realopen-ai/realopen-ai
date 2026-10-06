/** Configured voice first; browser speech is only a failure fallback. */
let activePlayer: ReadAloudPlayer | null = null;
export interface ReadingState {
  loading: boolean;
  paused: boolean;
  canSeek: boolean;
  speed: number;
}

export class ReadAloudPlayer {
  private controller: AbortController | null = null;
  private audio: HTMLAudioElement | null = null;
  private url: string | null = null;
  private utterance: SpeechSynthesisUtterance | null = null;
  private onDone: (() => void) | null = null;
  private onState: ((state: ReadingState) => void) | null = null;
  private state: ReadingState = {
    loading: true,
    paused: false,
    canSeek: false,
    speed: 1,
  };

  private update(patch: Partial<ReadingState>): void {
    this.state = { ...this.state, ...patch };
    this.onState?.(this.state);
  }

  seek(seconds: number): void {
    if (!this.audio || !Number.isFinite(this.audio.duration)) return;
    this.audio.currentTime = Math.max(
      0,
      Math.min(this.audio.duration, this.audio.currentTime + seconds),
    );
  }

  async togglePause(): Promise<void> {
    if (this.state.loading) return;
    if (this.state.paused) {
      if (this.audio) {
        try {
          await this.audio.play();
        } catch {
          this.stop();
          return;
        }
      } else window.speechSynthesis?.resume();
    } else if (this.audio) this.audio.pause();
    else window.speechSynthesis?.pause();
    if (this.controller) this.update({ paused: !this.state.paused });
  }

  setSpeed(speed: number): void {
    if (![1, 1.5, 2].includes(speed) || !this.audio) return;
    this.audio.playbackRate = speed;
    this.update({ speed });
  }

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
    this.onState = null;
    if (activePlayer === this) activePlayer = null;
    const onDone = this.onDone;
    this.onDone = null;
    onDone?.();
  }

  async play(
    text: string,
    onDone: () => void,
    onState?: (state: ReadingState) => void,
    options?: { localOnly?: boolean; onUnavailable?: () => void },
  ): Promise<void> {
    this.stop();
    activePlayer?.stop();
    activePlayer = this;
    this.onDone = onDone;
    this.onState = onState ?? null;
    this.update({ loading: true, paused: false, canSeek: false, speed: 1 });
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
      if (options?.localOnly) {
        options.onUnavailable?.();
        return finish();
      }
      this.audio?.pause();
      this.audio = null;
      if (this.url) URL.revokeObjectURL(this.url);
      this.url = null;
      if (!("speechSynthesis" in window)) return finish();
      const utterance = new SpeechSynthesisUtterance(text);
      this.utterance = utterance;
      this.update({ loading: false, paused: false, canSeek: false, speed: 1 });
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
      audio.onloadedmetadata = () => {
        if (!controller.signal.aborted && !fallingBack)
          this.update({ canSeek: Number.isFinite(audio.duration) });
      };
      audio.onended = finish;
      audio.onerror = fallback;
      await audio.play();
      if (!controller.signal.aborted && !fallingBack)
        this.update({
          loading: false,
          canSeek: Number.isFinite(audio.duration),
          speed: audio.playbackRate,
        });
    } catch {
      fallback();
    }
  }
}
