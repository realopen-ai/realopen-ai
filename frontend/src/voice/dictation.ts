/** One-shot recording; no voice session, agent generation, or playback. */
export function encodePcm(samples: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  samples.forEach((sample, i) => {
    const value = Math.max(-1, Math.min(1, sample));
    view.setInt16(i * 2, value * (value < 0 ? 32768 : 32767), true);
  });
  return buffer;
}

export function appendDictation(draft: string, text: string): string {
  const transcript = text.trim();
  return transcript
    ? `${draft}${draft && !/\s$/.test(draft) ? " " : ""}${transcript}`
    : draft;
}

/** Measured amplitude only: silence stays flat; stronger speech grows bars. */
export function microphoneLevel(samples: Float32Array): number {
  if (!samples.length) return 0;
  let sum = 0;
  for (const sample of samples) sum += sample * sample;
  return Math.min(1, Math.sqrt(sum / samples.length) * 5);
}

export class DictationRecorder {
  private stream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private cancelled = false;
  private context: AudioContext | null = null;
  private animation: number | null = null;

  private onLevel?: (level: number) => void;

  constructor(onLevel?: (level: number) => void) {
    this.onLevel = onLevel;
  }

  async start(): Promise<void> {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });
    if (this.cancelled) {
      stream.getTracks().forEach((track) => track.stop());
      throw new Error("Recording cancelled");
    }
    this.stream = stream;
    if (this.onLevel) {
      const context = new AudioContext();
      this.context = context;
      await context.resume();
      if (this.cancelled) throw new Error("Recording cancelled");
      const analyser = context.createAnalyser();
      analyser.fftSize = 2048;
      context.createMediaStreamSource(stream).connect(analyser);
      const samples = new Float32Array(analyser.fftSize);
      let last = 0;
      const measure = (time: number) => {
        if (this.cancelled || !this.context) return;
        if (time - last >= 60) {
          analyser.getFloatTimeDomainData(samples);
          this.onLevel?.(microphoneLevel(samples));
          last = time;
        }
        this.animation = requestAnimationFrame(measure);
      };
      this.animation = requestAnimationFrame(measure);
    }
    this.recorder = new MediaRecorder(stream);
    this.recorder.ondataavailable = (event) => {
      if (event.data.size) this.chunks.push(event.data);
    };
    this.recorder.start();
  }

  async stop(): Promise<ArrayBuffer> {
    const recorder = this.recorder;
    if (!recorder || recorder.state === "inactive")
      throw new Error("Not recording");
    await new Promise<void>((resolve, reject) => {
      recorder.onstop = () => resolve();
      recorder.onerror = () => reject(new Error("Recording failed"));
      recorder.stop();
    });
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stopMeter();
    const context = new AudioContext();
    try {
      const audio = await context.decodeAudioData(
        await new Blob(this.chunks).arrayBuffer(),
      );
      const offline = new OfflineAudioContext(
        1,
        Math.max(1, Math.ceil(audio.duration * 16000)),
        16000,
      );
      const source = offline.createBufferSource();
      source.buffer = audio;
      source.connect(offline.destination);
      source.start();
      const resampled = await offline.startRendering();
      return encodePcm(resampled.getChannelData(0));
    } finally {
      await context.close();
    }
  }

  dispose(): void {
    this.cancelled = true;
    if (this.recorder?.state === "recording") this.recorder.stop();
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stopMeter();
  }

  private stopMeter(): void {
    if (this.animation !== null) cancelAnimationFrame(this.animation);
    this.animation = null;
    void this.context?.close().catch(() => {});
    this.context = null;
  }
}
