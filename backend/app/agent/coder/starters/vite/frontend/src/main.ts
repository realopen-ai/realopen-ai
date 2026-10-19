import "./style.css";
import { request } from "./api";
import { showStatus } from "./status";

const status = document.querySelector<HTMLParagraphElement>("#status")!;
void showStatus(status, async () => {
  const data = await request<{ status: string }>("/api/health");
  return `API: ${data.status}`;
});
