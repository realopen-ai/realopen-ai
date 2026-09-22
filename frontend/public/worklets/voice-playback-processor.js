// voice-playback-processor.js — AudioWorkletProcessor (plain JS, runs on the
// audio render thread).
//
// Responsibilities:
//   • Maintain an internal FIFO of Int16 PCM frames at 24000 Hz (the TTS
//     output rate).
//   • Play them by converting to Float32 at the AudioContext rate with
//     linear resampling.
//   • Every EXACT frame taken off the queue for playback (the samples the
//     read position has fully advanced past — the audio actually scheduled
//     to the speaker) is posted back to the main thread as
//     `{ type: 'played', buffer: ArrayBuffer }` (Int16 @ 24 kHz). That is
//     the far-end reference the client echoes to the server (0x02 frames,
//     downsampled to 16 kHz on the main thread) for AEC — it mirrors the
//     real speaker output, never the input queue.
//   • Messages from the main thread:
//       { type: 'append', buffer: ArrayBuffer, sampleRate?: number }
//           Enqueue a chunk of Int16 PCM. Defaults to 24000 Hz; a different
//           rate is linearly resampled into the 24 kHz queue.
//       { type: 'clear' }
//           Flush the queue instantly (barge-in). Flushed audio was never
//           scheduled to the speaker, so it is NOT echoed back as 'played'.
//   • Output silence while the queue underruns (continuous playback).
//
// Registered as 'voice-playback-processor'.

const QUEUE_RATE = 24000;

class VoicePlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    /** @type {Int16Array[]} FIFO of Int16 chunks at QUEUE_RATE. */
    this._chunks = [];
    /** Integer read index into this._chunks[0] (the "in use" sample). */
    this._idx = 0;
    /** Fractional read position within the sample at _idx. */
    this._frac = 0;
    /** Input samples consumed per output sample (24k → context rate). */
    this._step = QUEUE_RATE / sampleRate;
    this._speed = 1;
    /** Samples fully consumed since the last 'played' post. */
    this._played = [];

    this.port.onmessage = (e) => {
      const msg = e.data;
      if (!msg) return;
      if (msg.type === "append" && msg.buffer) {
        const chunk = new Int16Array(msg.buffer);
        if (chunk.length === 0) return;
        const rate =
          typeof msg.sampleRate === "number" ? msg.sampleRate : QUEUE_RATE;
        if (rate === QUEUE_RATE) {
          this._chunks.push(chunk);
        } else {
          // Defensive path: linearly resample the incoming chunk to 24 kHz
          // before enqueuing (the protocol says 0x03 is 24000 Hz, but the
          // tts_chunk announce carries the authoritative rate).
          const ratio = rate / QUEUE_RATE;
          const outLen = Math.max(1, Math.floor(chunk.length / ratio));
          const out = new Int16Array(outLen);
          let pos = 0;
          for (let i = 0; i < outLen; i++) {
            const i0 = Math.floor(pos);
            const i1 = Math.min(i0 + 1, chunk.length - 1);
            const f = pos - i0;
            const v = chunk[i0] * (1 - f) + chunk[i1] * f;
            out[i] = v < 0 ? Math.max(-32768, v) : Math.min(32767, v);
            pos += ratio;
          }
          this._chunks.push(out);
        }
      } else if (msg.type === "speed") {
        const value = Number(msg.value);
        this._speed = [0.5, 1, 1.5, 2].includes(value) ? value : 1;
      } else if (msg.type === "clear") {
        // Barge-in: drop everything. Flushed samples were never scheduled
        // to the speaker, so they are NOT reported as 'played'.
        this._chunks = [];
        this._idx = 0;
        this._frac = 0;
        this._played = [];
      }
    };
  }

  /** Whether at least one sample is available for playback. */
  _hasData() {
    return this._chunks.length > 0 && this._idx < this._chunks[0].length;
  }

  /** Sample at the fractional read position (linear interpolation; the end
   *  sample may live at the head of the next chunk). */
  _readSample() {
    const chunk = this._chunks[0];
    const s0 = chunk[this._idx];
    let s1;
    if (this._idx + 1 < chunk.length) {
      s1 = chunk[this._idx + 1];
    } else if (this._chunks.length > 1) {
      s1 = this._chunks[1][0];
    } else {
      s1 = s0;
    }
    return s0 * (1 - this._frac) + s1 * this._frac;
  }

  /** Advance the read position by _step input samples. Each integer step
   *  leaves one sample fully behind — that sample is collected as
   *  'played' (far-end reference), and exhausted chunks are dropped. */
  _advance() {
    this._frac += this._step * this._speed;
    while (this._frac >= 1 && this._chunks.length > 0) {
      this._frac -= 1;
      const chunk = this._chunks[0];
      if (this._idx < chunk.length) {
        this._played.push(chunk[this._idx]);
      }
      this._idx++;
      if (this._idx >= chunk.length) {
        // Chunk fully consumed — drop it and continue at the next chunk.
        this._chunks.shift();
        this._idx = 0;
      }
    }
  }

  /** Post the samples consumed during this block back to the main thread. */
  _flushPlayed() {
    if (this._played.length === 0) return;
    const buf = new Int16Array(this._played);
    this._played = [];
    this.port.postMessage({ type: "played", buffer: buf.buffer }, [buf.buffer]);
  }

  process(_inputs, outputs) {
    const output = outputs[0];
    if (!output || output.length === 0 || !output[0]) return true;
    const channel = output[0];

    for (let i = 0; i < channel.length; i++) {
      if (this._hasData()) {
        channel[i] = this._readSample() / 32768;
        this._advance();
      } else {
        // Underrun — emit silence (continuous playback).
        channel[i] = 0;
      }
    }

    // Report the exact samples scheduled during this block, in lockstep.
    this._flushPlayed();
    return true;
  }
}

registerProcessor("voice-playback-processor", VoicePlaybackProcessor);
