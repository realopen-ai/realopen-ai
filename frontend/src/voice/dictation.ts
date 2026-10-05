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

export class DictationRecorder {
  private stream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private cancelled = false;

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
  }
}
