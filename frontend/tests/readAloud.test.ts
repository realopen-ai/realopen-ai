import { test } from "node:test";
import assert from "node:assert/strict";
import { ReadAloudPlayer } from "../src/voice/readAloud.ts";

test("reading controls clamp seeks, pause/resume, change speed and close on end", async () => {
  const originalFetch = globalThis.fetch;
  const originalAudio = globalThis.Audio;
  let audio!: FakeAudio;
  let finished = 0;
  let paused = false;
  let state: any;
  class FakeAudio {
    src = "";
    currentTime = 3;
    duration = 12;
    playbackRate = 1;
    onended?: () => void;
    constructor() {
      audio = this;
    }
    play() {
      paused = false;
      return Promise.resolve();
    }
    pause() {
      paused = true;
    }
  }
  globalThis.Audio = FakeAudio as unknown as typeof Audio;
  globalThis.fetch = async () => new Response(new Blob(["wav"]));
  try {
    const player = new ReadAloudPlayer();
    await player.play(
      "Hello",
      () => finished++,
      (value) => {
        state = value;
      },
    );
    assert.equal(state.loading, false);
    assert.equal(state.canSeek, true);
    player.seek(-5);
    assert.equal(audio.currentTime, 0);
    player.seek(5);
    assert.equal(audio.currentTime, 5);
    player.seek(100);
    assert.equal(audio.currentTime, 12);
    await player.togglePause();
    assert.equal(paused, true);
    assert.equal(state.paused, true);
    await player.togglePause();
    assert.equal(state.paused, false);
    player.setSpeed(2);
    assert.equal(audio.playbackRate, 2);
    assert.equal(state.speed, 2);
    audio.onended?.();
    assert.equal(finished, 1);
    player.stop();
    assert.equal(finished, 1);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.Audio = originalAudio;
  }
});

test("configured speech uses saved speed, and stop releases audio", async () => {
  const originalFetch = globalThis.fetch;
  const originalAudio = globalThis.Audio;
  let paused = false;
  let rate = 0;
  globalThis.fetch = async () =>
    new Response(new Blob(["wav"]), {
      headers: { "X-Playback-Speed": "1.5" },
    });
  globalThis.Audio = class {
    src = "";
    set playbackRate(value: number) {
      rate = value;
    }
    play() {
      return Promise.resolve();
    }
    pause() {
      paused = true;
    }
  } as unknown as typeof Audio;
  try {
    const player = new ReadAloudPlayer();
    await player.play("Hello", () => {});
    assert.equal(rate, 1.5);
    player.stop();
    assert.equal(paused, true);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.Audio = originalAudio;
  }
});

test("runtime failure falls back, but cancelled requests never speak", async () => {
  const originalFetch = globalThis.fetch;
  const originals = [globalThis.window, globalThis.SpeechSynthesisUtterance];
  let spoken = 0;
  globalThis.window = {
    speechSynthesis: {
      cancel() {},
      speak() {
        spoken++;
      },
    },
  } as unknown as Window & typeof globalThis;
  globalThis.SpeechSynthesisUtterance =
    class {} as typeof SpeechSynthesisUtterance;
  try {
    globalThis.fetch = async () => new Response(null, { status: 503 });
    const player = new ReadAloudPlayer();
    await player.play("Hello", () => {});
    assert.equal(spoken, 1);
    let reject!: (error: Error) => void;
    globalThis.fetch = () =>
      new Promise((_, fail) => {
        reject = fail;
      });
    const pending = player.play("Cancelled", () => {});
    player.stop();
    reject(new Error("aborted"));
    await pending;
    assert.equal(spoken, 1);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originals[0] as Window & typeof globalThis;
    globalThis.SpeechSynthesisUtterance =
      originals[1] as typeof SpeechSynthesisUtterance;
  }
});
