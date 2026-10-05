import { test } from "node:test";
import assert from "node:assert/strict";
import {
  appendDictation,
  encodePcm,
  DictationRecorder,
} from "../src/voice/dictation.ts";

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
