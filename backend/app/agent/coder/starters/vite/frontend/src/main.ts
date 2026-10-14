import "@picocss/pico/css/pico.min.css";
import "./style.css";
import { request } from "./api";

const status = document.querySelector<HTMLParagraphElement>("#status")!;
request<{ status: string }>("/api/health")
  .then((data) => {
    status.textContent = `API: ${data.status}`;
  })
  .catch((error: Error) => {
    status.textContent = error.message;
  });
