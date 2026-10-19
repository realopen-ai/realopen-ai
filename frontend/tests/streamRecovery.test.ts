import assert from "node:assert/strict";
import test from "node:test";
import "./helpers/viteCompat.ts";
import { installFetch, sseResponse } from "./helpers/fetchMock.ts";
const { streamChat, streamChatWithFiles, resumeChatStream, stopActiveStream } =
  await import("../src/api/stream.ts");

const frame = (id: number, event: unknown) =>
  `id: ${id}\ndata: ${JSON.stringify(event)}\n\n`;
const tick = () => new Promise<void>((resolve) => setImmediate(resolve));
function recorder() {
  const tokens: string[] = [],
    thinking: string[] = [],
    errors: string[] = [],
    activities: boolean[] = [];
  let done = 0,
    started = 0;
  return {
    tokens,
    thinking,
    errors,
    activities,
    get done() {
      return done;
    },
    get started() {
      return started;
    },
    callbacks: {
      onToken(token: string) {
        tokens.push(token);
      },
      onThinkingStart() {
        started++;
      },
      onThinkingToken(token: string) {
        thinking.push(token);
      },
      onThinkingDone() {},
      onGenerationDone() {},
      onToolCallStart() {},
      onToolCallUpdate() {},
      onAgentActivity(_activity: unknown, active: boolean) {
        activities.push(active);
      },
      onDone() {
        done++;
      },
      onError(error: string) {
        errors.push(error);
      },
    },
  };
}

test("active SSE survives more than 30 minutes and subsequent compaction timeout recovery", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const received = recorder();
  let body!: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        body = c;
      },
    }),
  );
  const mock = installFetch(() => response);
  const running = streamChat([], "coder", received.callbacks, {
    conversationId: "long",
  });
  try {
    body.enqueue(
      encoder.encode(
        frame(1, {
          event: "agent_activity",
          activity: "context_compaction",
          active: true,
        }),
      ),
    );
    await tick();
    for (let i = 0; i < 4; i++) {
      t.mock.timers.tick(9 * 60 * 1000);
      body.enqueue(encoder.encode(": keep-alive\n\n"));
      await tick();
    }
    assert.deepEqual(received.errors, []);
    assert.equal(received.done, 0);
    body.enqueue(
      encoder.encode(
        frame(2, {
          event: "agent_activity",
          activity: "context_compaction",
          active: false,
        }) +
          frame(3, { event: "thinking_start", agent: "coder", round: 18 }) +
          frame(4, {
            event: "thinking",
            thinking: "Continue after a recoverable summary timeout",
          }) +
          frame(5, { event: "message", message: { content: "Tests passed" } }) +
          "id: 6\ndata: [DONE]\n\n",
      ),
    );
    await running;
    assert.deepEqual(received.activities, [true, false]);
    assert.equal(received.started, 1);
    assert.deepEqual(received.tokens, ["Tests passed"]);
    assert.equal(received.done, 1);
    assert.deepEqual(received.errors, []);
  } finally {
    body.close();
    mock.restore();
  }
});

for (const multipart of [false, true]) {
  test(`${multipart ? "multipart" : "JSON"} stream reconnects without resending the request or duplicating events`, async () => {
    const received = recorder();
    const mock = installFetch((url) =>
      url.includes("/events?")
        ? sseResponse([
            frame(2, {
              event: "agent_activity",
              activity: "context_compaction",
              active: true,
            }),
            frame(3, {
              event: "agent_activity",
              activity: "context_compaction",
              active: false,
            }),
            frame(4, { event: "message", message: { content: "continued" } }),
            "id: 5\ndata: [DONE]\n\n",
          ])
        : sseResponse([
            frame(1, { event: "message", message: { content: "before" } }),
            frame(2, {
              event: "agent_activity",
              activity: "context_compaction",
              active: true,
            }),
          ]),
    );
    try {
      if (multipart)
        await streamChatWithFiles(
          [],
          "coder",
          received.callbacks,
          {},
          { conversationId: "qa" },
        );
      else
        await streamChat([], "coder", received.callbacks, {
          conversationId: "qa",
        });
      assert.deepEqual(received.tokens, ["before", "continued"]);
      assert.deepEqual(received.activities, [true, false]);
      assert.deepEqual(received.errors, []);
      assert.equal(received.done, 1);
      assert.equal(
        mock.calls.filter((call) => call.init?.method === "POST").length,
        1,
      );
      assert.equal(mock.calls[1].url, "/api/chat/stream/qa/events?after=2");
    } finally {
      mock.restore();
    }
  });
}

test("reload reconnect starts from the persisted checkpoint even if the first socket closes empty", async () => {
  const received = recorder();
  let connected = 0;
  const mock = installFetch((url) => {
    if (url.endsWith("/events")) {
      const response = sseResponse([]);
      response.headers.set("X-Stream-Cursor", "12");
      return response;
    }
    assert.equal(url, "/api/chat/stream/reload/events?after=12");
    return sseResponse([
      frame(13, { event: "message", message: { content: "tail" } }),
      "data: [DONE]\n\n",
    ]);
  });
  try {
    assert.equal(
      await resumeChatStream("reload", () => {
        connected++;
        return received.callbacks;
      }),
      true,
    );
    assert.equal(connected, 1);
    assert.deepEqual(received.tokens, ["tail"]);
    assert.equal(received.done, 1);
  } finally {
    mock.restore();
  }
});

test("unrecoverable disconnect reports an error rather than claiming completion", async () => {
  const received = recorder();
  const mock = installFetch(() => sseResponse([]));
  try {
    await streamChat([], "coder", received.callbacks, {
      conversationId: "dead",
    });
    assert.equal(mock.calls.length, 4);
    assert.equal(received.done, 0);
    assert.match(received.errors[0], /closed before completion/);
  } finally {
    mock.restore();
  }
});

test("user stop during reconnect cancels retries without a false error", async () => {
  const received = recorder();
  const mock = installFetch(() => sseResponse([]));
  try {
    const running = streamChat([], "coder", received.callbacks, {
      conversationId: "stop",
    });
    await tick();
    await stopActiveStream();
    await running;
    assert.equal(mock.calls.length, 1);
    assert.deepEqual(received.errors, []);
    assert.equal(received.done, 1);
  } finally {
    mock.restore();
  }
});

test("automatic reconnect never mixes a replacement execution into the original reply", async () => {
  const received = recorder();
  const mock = installFetch((url) => {
    const response = sseResponse(
      url.includes("/events?")
        ? [
            frame(1, {
              event: "message",
              message: { content: "another request" },
            }),
            "data: [DONE]\n\n",
          ]
        : [],
    );
    response.headers.set(
      "X-Stream-ID",
      url.includes("/events?") ? "replacement" : "original",
    );
    return response;
  });
  try {
    await streamChat([], "coder", received.callbacks, { conversationId: "qa" });
    assert.deepEqual(received.tokens, []);
    assert.equal(received.done, 0);
    assert.match(received.errors[0], /refusing to mix responses/);
  } finally {
    mock.restore();
  }
});

test("partial frame at disconnect is replayed from the last completely dispatched event", async () => {
  const received = recorder();
  const mock = installFetch((url) => {
    if (url.includes("/events?")) {
      assert.equal(url, "/api/chat/stream/qa/events?after=1");
      return sseResponse([
        frame(2, { event: "message", message: { content: "second" } }),
        "data: [DONE]\n\n",
      ]);
    }
    return sseResponse([
      frame(1, { event: "message", message: { content: "first" } }),
      'id: 2\ndata: {"event":"message","message":',
    ]);
  });
  try {
    await streamChat([], "coder", received.callbacks, { conversationId: "qa" });
    assert.deepEqual(received.tokens, ["first", "second"]);
    assert.equal(received.done, 1);
  } finally {
    mock.restore();
  }
});
