import { test } from "node:test";
import assert from "node:assert/strict";
import {
  appendDictation,
  encodePcm,
  DictationRecorder,
  microphoneLevel,
} from "../src/voice/dictation.ts";

test("waveform reflects measured speech energy, not synthetic animation", () => {
  assert.equal(microphoneLevel(new Float32Array(2048)), 0);
  assert.equal(microphoneLevel(new Float32Array()), 0);
  const quiet = microphoneLevel(new Float32Array([0.01, -0.01]));
  const speech = microphoneLevel(new Float32Array([0.1, -0.1]));
  assert.ok(speech > quiet);
  assert.equal(microphoneLevel(new Float32Array([1, -1])), 1);
});

test("dictation preserves drafts and ignores empty transcripts", () => {
  assert.equal(appendDictation("Hello", " world "), "Hello world");
  assert.equal(appendDictation("Hello\n", "world"), "Hello\nworld");
  assert.equal(appendDictation("Hello", "  "), "Hello");
  assert.equal(appendDictation("", " test "), "test");
});

test("leaving during microphone permission releases late-arriving tracks", async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  let stopped = false;
  let grant!: (stream: MediaStream) => void;
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      mediaDevices: {
        getUserMedia: () =>
          new Promise<MediaStream>((resolve) => {
            grant = resolve;
          }),
      },
    },
  });
  try {
    const recorder = new DictationRecorder();
    const pending = recorder.start();
    recorder.dispose();
    grant({
      getTracks: () => [
        {
          stop: () => {
            stopped = true;
          },
        },
      ],
    } as unknown as MediaStream);
    await assert.rejects(pending, /cancelled/);
    assert.equal(stopped, true);
  } finally {
    if (original) Object.defineProperty(globalThis, "navigator", original);
    else Reflect.deleteProperty(globalThis, "navigator");
  }
});

test("dictation encodes clamped signed 16-bit little-endian audio", () => {
  const pcm = encodePcm(new Float32Array([-2, -1, 0, 1, 2]));
  const view = new DataView(pcm);
  assert.deepEqual(
    Array.from({ length: 5 }, (_, i) => view.getInt16(i * 2, true)),
    [-32768, -32768, 0, 32767, 32767],
  );
});
