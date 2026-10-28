import { useEffect, useState } from "react";
import { request } from "./api";

export function App({ connected = true }: { connected?: boolean }) {
  const [status, setStatus] = useState(connected ? "Loading…" : "Ready");
  const [loading, setLoading] = useState(connected);

  useEffect(() => {
    if (!connected) {
      setLoading(false);
      setStatus("Ready");
      return;
    }
    let current = true;
    const controller = new AbortController();
    setLoading(true);
    request<{ status: string }>("/api/health", { signal: controller.signal })
      .then((data) => {
        if (current) setStatus(`API: ${data.status}`);
      })
      .catch((error) => {
        if (current)
          setStatus(error instanceof Error ? error.message : "Request failed");
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [connected]);

  return (
    <main className="container">
      <h1>Workspace app</h1>
      <p role="status" aria-busy={loading}>
        {status}
      </p>
      <section aria-label="Application">
        <p>Your application starts here.</p>
      </section>
    </main>
  );
}
