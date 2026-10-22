export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const text = await response.text();
  let body: unknown = undefined;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? (body as { detail?: unknown }).detail
        : undefined;
    throw new ApiError(
      typeof detail === "string"
        ? detail
        : typeof body === "string" && body
          ? body
          : `Request failed (${response.status})`,
      response.status,
    );
  }
  // DELETE/PUT endpoints commonly return 204 or an empty successful body.
  // Callers should not fail after the server has already applied the change.
  return body as T;
}
