# Production gateway

Nginx serves the built frontend and proxies backend traffic in production. Development uses Vite instead; this service is disabled by the development Compose override.

## Files

- [Dockerfile](Dockerfile): builds the frontend from the repository root, then copies its static output into an Nginx image.
- [nginx.conf](nginx.conf): upstream configuration, SPA routing, API/SSE/WebSocket forwarding, headers, compression, rate limits, and upload limits.

## Run

From the repository root:

```sh
make up
```

The production gateway listens on container port **80**, published as `${NGINX_PORT:-80}` by Compose.

To build only the gateway image:

```sh
docker build -f nginx/Dockerfile -t realopenai-nginx:local .
```

The build context must be the repository root because the image includes `frontend/`.

## Proxy behavior

| Route | Behavior |
| --- | --- |
| `/api/` | Forward HTTP API requests to `backend:8000` without response buffering |
| `/api/chat/stream` | Forward SSE with buffering/cache disabled and long read timeout |
| `/api/sandboxes/<id>/terminal` | Explicit WebSocket upgrade for sandbox terminal connections |
| `/ws/` | WebSocket upgrades, including voice calls |
| Frontend routes | Serve static content with SPA fallback |

Keep the sandbox terminal WebSocket location separate from ordinary API forwarding: the latter clears the `Connection` header. Buffering SSE or dropping upgrade headers breaks streaming and terminal sessions.

## Validation and deployment

With the production service running:

```sh
docker compose exec nginx nginx -t
```

Rebuild/recreate the service after changing its bundled configuration or frontend; those files are copied into the image.

The shipped server is HTTP-only and intended for local use. Before public exposure, configure TLS and access controls, review forwarded headers and rate limits, and secure the backend/monitoring services. Upload size is capped at **50 MB** by this gateway.
