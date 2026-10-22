import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { createContext } from "../extensions";
import { install } from "./session";
import { Boundary } from "./Boundary";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("gates protected content, handles invalid credentials, signs in and signs out", async () => {
  let signedIn = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      if (path.endsWith("session")) return new Response(null, { status: 401 });
      if (path.endsWith("logout")) {
        signedIn = false;
        return new Response(null, { status: 204 });
      }
      const credentials = JSON.parse(String(init?.body));
      if (credentials.password === "wrong-password-value")
        return Response.json(
          { detail: "Invalid username or password" },
          { status: 401 },
        );
      signedIn = true;
      return Response.json({
        user: { id: 1, username: credentials.username },
        csrf_token: "token",
      });
    }),
  );
  const context = createContext();
  const close = install(context);
  const rendered = render(
    <Boundary context={context}>
      <p>Protected records</p>
    </Boundary>,
  );
  expect(screen.queryByText("Protected records")).toBeNull();
  await screen.findByRole("heading", { name: "Sign in" });
  fireEvent.change(screen.getByLabelText("Username"), {
    target: { value: "alice" },
  });
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: "wrong-password-value" },
  });
  fireEvent.submit(screen.getByRole("form"));
  await screen.findByRole("alert");
  expect(screen.queryByText("Protected records")).toBeNull();
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: "correct-long-password" },
  });
  fireEvent.submit(screen.getByRole("form"));
  await screen.findByText("Protected records");
  expect(signedIn).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  await waitFor(() =>
    expect(screen.queryByText("Protected records")).toBeNull(),
  );
  expect(signedIn).toBe(false);
  rendered.unmount();
  close();
});
