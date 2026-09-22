// voice-capture-processor.js — AudioWorkletProcessor (plain JS, runs on the
// audio render thread).
//
// Responsibilities:
//   • Consume mono Float32 channel data at the AudioContext sample rate.
//   • Downsample to 16000 Hz with simple decimation + averaging (a
//     fractional accumulator keeps the timing drift-free for non-integer
//     ratios such as 44100/16000).
//   • Convert to Int16 PCM.
//   • Buffer ~100 ms (1600 samples = 3200 bytes) and post it to the main
//     thread as `{ type: 'pcm', buffer: ArrayBuffer }`.
//
// IMPORTANT: this processor NEVER gates on playback state. The microphone
// stream must keep flowing while the assistant audio plays so that
// barge-in (user speaking over the assistant) works and so the server
// receives the near-end signal for AEC at all times.
//
// Registered as 'voice-capture-processor'.

const TARGET_RATE = 16000;
// ~100 ms of 16 kHz mono s16le audio per posted frame (spec: 3200 bytes).
const FRAME_SAMPLES = 1600;

class VoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._ratio = sampleRate / TARGET_RATE; // input samples per output sample
    // Fractional progress toward the next output sample. Carries across
    // process() calls so there is no long-term drift.
    this._frac = 0;
    // Running average accumulators for the current output sample window.
    this._acc = 0;
    this._accCount = 0;
    // Output Int16 buffer (~100 ms).
    this._out = new Int16Array(FRAME_SAMPLES);
    this._outFill = 0;
  }

  /** Feed one window of input samples (averaging decimation). */
  _processChannel(channel) {
    for (let i = 0; i < channel.length; i++) {
      const s = channel[i];
      this._acc += s;
      this._accCount++;
      this._frac += 1 / this._ratio;
      if (this._frac >= 1) {
        // Emit one output sample: the average of the input samples that
        // fell inside this output-sample window.
        let v = this._accCount > 0 ? this._acc / this._accCount : 0;
        // Clamp before Int16 conversion.
        if (v > 1) v = 1;
        else if (v < -1) v = -1;
        this._out[this._outFill++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        // Reset window accumulators.
        this._acc = 0;
        this._accCount = 0;
        this._frac -= 1;
        if (this._outFill === FRAME_SAMPLES) {
          // Post a copy (transferred) so we can keep reusing _out.
          const copy = new Int16Array(this._out);
          this.port.postMessage({ type: "pcm", buffer: copy.buffer }, [
            copy.buffer,
          ]);
          this._outFill = 0;
        }
      }
    }
  }

  process(inputs) {
    // The capture node is fed by a mono MediaStreamSource; take channel 0.
    // If the input goes away (track ended) we simply output true to keep
    // the processor alive — the main thread owns the lifecycle.
    const input = inputs[0];
    if (input && input.length > 0 && input[0]) {
      this._processChannel(input[0]);
    }
    return true;
  }
}

registerProcessor("voice-capture-processor", VoiceCaptureProcessor);
