import type { AppContext, SessionSnapshot } from "../extensions";
import { ApiError } from "../api";

type SessionResult = {
  user: { id: number; username: string };
  csrf_token: string;
};

export function install(context: AppContext) {
  const previous = context.request;
  const subscribers = new Set<() => void>();
  let snapshot: SessionSnapshot = { status: "loading", user: null, error: "" };
  let csrf = "",
    disposed = false,
    version = 0;
  function update(next: SessionSnapshot) {
    if (disposed) return;
    snapshot = next;
    subscribers.forEach((listener) => listener());
  }
  function anonymous(error = "") {
    csrf = "";
    update({ status: "anonymous", user: null, error });
  }
  async function authRequest(
    action: string,
    body?: unknown,
  ): Promise<SessionResult | undefined> {
    const headers = new Headers({ "X-App-Request": "1" });
    if (csrf) headers.set("X-CSRF-Token", csrf);
    if (body) headers.set("Content-Type", "application/json");
    const response = await fetch("/api/auth/" + action, {
      method: body || action === "logout" ? "POST" : "GET",
      credentials: "same-origin",
      headers,
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (
      response.status === 401 &&
      (action === "session" || action === "logout")
    )
      return undefined;
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(
        typeof detail.detail === "string"
          ? detail.detail
          : "Authentication failed",
      );
    }
    if (response.status === 204) return undefined;
    const result = await response.json();
    if (
      !result ||
      !Number.isSafeInteger(result.user?.id) ||
      result.user.id < 1 ||
      typeof result.user.username !== "string" ||
      typeof result.csrf_token !== "string" ||
      !result.csrf_token
    ) {
      throw new Error("Invalid session response; try again");
    }
    return result;
  }
  function accept(result: SessionResult | undefined) {
    if (!result) return anonymous();
    csrf = result.csrf_token;
    update({ status: "authenticated", user: result.user, error: "" });
  }
  const session = {
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) {
      subscribers.add(listener);
      return () => {
        subscribers.delete(listener);
      };
    },
    async refresh() {
      const current = ++version;
      update({ status: "loading", user: null, error: "" });
      try {
        const result = await authRequest("session");
        if (current === version && !disposed) accept(result);
      } catch (error) {
        if (current === version)
          update({
            status: "error",
            user: null,
            error: String(error instanceof Error ? error.message : error),
          });
      }
    },
    async authenticate(
      action: "login" | "register",
      username: string,
      password: string,
    ) {
      const current = ++version;
      const result = await authRequest(action, { username, password });
      if (current === version && !disposed) accept(result);
    },
    async logout() {
      const current = ++version;
      await authRequest("logout");
      if (current === version) anonymous();
    },
  };
  const transport: AppContext["request"] = async <T,>(
    path: string,
    init?: RequestInit,
  ): Promise<T> => {
    if (!path.startsWith("/api/")) return previous<T>(path, init);
    const current = version;
    const headers = new Headers(init?.headers);
    headers.set("X-App-Request", "1");
    if (csrf) headers.set("X-CSRF-Token", csrf);
    // Expired sessions remove protected UI immediately. Validation/network errors
    // keep the current form intact; no failed mutation is automatically replayed.
    try {
      return await previous<T>(path, {
        ...init,
        credentials: "same-origin",
        headers,
      });
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status === 401 &&
        current === version &&
        !disposed
      ) {
        ++version;
        anonymous("Your session expired; sign in again");
      }
      throw error;
    }
  };
  context.session = session;
  context.request = transport;
  void session.refresh();
  return () => {
    disposed = true;
    ++version;
    subscribers.clear();
    if (context.request === transport) context.request = previous;
    if (context.session === session) delete context.session;
  };
}
