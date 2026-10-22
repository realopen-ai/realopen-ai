import {
  useState,
  useSyncExternalStore,
  type FormEvent,
  type ReactNode,
} from "react";
import type { AppContext } from "../extensions";

export function Boundary({
  context,
  children,
}: {
  context: AppContext;
  children: ReactNode;
}) {
  const session = context.session!;
  const snapshot = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [register, setRegister] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      await session.authenticate(
        register ? "register" : "login",
        String(form.get("username")),
        String(form.get("password")),
      );
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Authentication failed",
      );
    } finally {
      setBusy(false);
    }
  }
  if (snapshot.status === "loading")
    return (
      <main className="container">
        <p role="status">Checking session…</p>
      </main>
    );
  if (snapshot.status === "error")
    return (
      <main className="container">
        <p role="alert">{snapshot.error}</p>
        <button onClick={() => void session.refresh()}>Retry</button>
      </main>
    );
  if (snapshot.status === "authenticated")
    return (
      <>
        <header className="container">
          <span>Signed in as {snapshot.user!.username}</span>
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError("");
              try {
                await session.logout();
              } catch (error) {
                setError(
                  error instanceof Error ? error.message : "Sign out failed",
                );
              } finally {
                setBusy(false);
              }
            }}
          >
            Sign out
          </button>
          {error && <p role="alert">{error}</p>}
        </header>
        <section key={snapshot.user!.id}>{children}</section>
      </>
    );
  return (
    <main className="container">
      <h1>{register ? "Create account" : "Sign in"}</h1>
      <form
        onSubmit={submit}
        aria-label={register ? "Create account" : "Sign in"}
      >
        <label>
          Username
          <input
            name="username"
            required
            minLength={3}
            maxLength={48}
            autoComplete="username"
            disabled={busy}
          />
        </label>
        <label>
          Password
          <input
            name="password"
            type="password"
            required
            minLength={12}
            maxLength={128}
            autoComplete={register ? "new-password" : "current-password"}
            disabled={busy}
          />
        </label>
        <p>Use a password of at least 12 characters.</p>
        {(error || snapshot.error) && (
          <p role="alert">{error || snapshot.error}</p>
        )}
        <button type="submit" disabled={busy}>
          {busy ? "Please wait…" : register ? "Create account" : "Sign in"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setRegister(!register);
            setError("");
          }}
        >
          {register ? "Already have an account?" : "Create an account"}
        </button>
      </form>
    </main>
  );
}
