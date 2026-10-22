import { afterEach, expect, it, vi } from "vitest";
import { createContext } from "../extensions";
import { install } from "./session";
import { mount } from "./mount";

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

it("mounts records only after login and preserves the sign-out control during app renders", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) =>
      path.endsWith("session")
        ? new Response(null, { status: 401 })
        : path.endsWith("logout")
          ? new Response(null, { status: 204 })
          : Response.json({
              user: { id: 1, username: "alice" },
              csrf_token: "token",
            }),
    ),
  );
  const context = createContext();
  const closePlugin = install(context);
  const root = document.createElement("main");
  document.body.append(root);
  const dispose = vi.fn();
  let appRoot: HTMLElement;
  const close = mount(root, context, (target) => {
    appRoot = target;
    target.textContent = "Protected";
    return dispose;
  });
  await settle();
  expect(root.textContent).not.toContain("Protected");
  await context.session!.authenticate(
    "login",
    "alice",
    "correct-long-password",
  );
  appRoot!.replaceChildren(document.createTextNode("Rendered again"));
  expect(root.textContent).toContain("Sign out");
  await context.session!.logout();
  expect(root.textContent).not.toContain("Rendered again");
  expect(dispose).toHaveBeenCalledOnce();
  close();
  closePlugin();
});
