import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("shows API health and clears loading", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ status: "ok" }))),
  );
  render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
  await screen.findByText("API: ok");
  await waitFor(() =>
    expect(screen.getByRole("status").getAttribute("aria-busy")).toBe("false"),
  );
});

it("shows failures without leaving a loading flag", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Offline")));
  render(<App />);
  await screen.findByText("Offline");
  expect(screen.getByRole("status").getAttribute("aria-busy")).toBe("false");
});

it("does not invent an API for a frontend-only project", () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  render(<App connected={false} />);
  expect(screen.getByRole("status").textContent).toBe("Ready");
  expect(fetch).not.toHaveBeenCalled();
});
