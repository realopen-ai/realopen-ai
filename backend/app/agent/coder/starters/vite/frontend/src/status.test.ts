import { expect, test } from "vitest";
import { showStatus } from "./status";

test("shows loading only until the request finishes", async () => {
  document.body.innerHTML = '<p id="status"></p>';
  const status = document.querySelector<HTMLElement>("#status")!;
  let finish!: (value: string) => void;
  const pending = showStatus(
    status,
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  expect(status.textContent).toBe("Loading…");
  expect(status.getAttribute("aria-busy")).toBe("true");
  finish("Ready");
  await pending;
  expect(status.textContent).toBe("Ready");
  expect(status.hasAttribute("aria-busy")).toBe(false);
});

test("clears loading and displays a failed request", async () => {
  const status = document.createElement("p");
  await showStatus(status, async () => {
    throw new Error("Offline");
  });
  expect(status.textContent).toBe("Offline");
  expect(status.hasAttribute("aria-busy")).toBe(false);
});
