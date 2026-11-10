# BidDesk React Workspace (2A-0)

Minimal React/TypeScript/Vite entry for authentication and permission-checked reads. The access token remains in memory; reloading requires signing in again. No model key is needed for frontend type checking or building.

## Development

Start the configured FastAPI service on `127.0.0.1:8000`, then run:

```sh
npm ci
npm run dev
```

Vite proxies `/auth` and `/api` to FastAPI. Set Vite's proxy target in `vite.config.ts` when the API listens elsewhere. The configured login accounts remain server-side; never add passwords or signing secrets to this frontend.

## Current scope

- Login, identity verification, protected workspace route, and logout.
- Draft JSON read and material binary download through the existing protected API.
- `401` clears the in-memory identity; `404` does not distinguish missing objects from denied access.
- Restricted responses are not cached by the client.
- SSE is not implemented here. Future subscriptions must use an Authorization Bearer header, recheck current access while streaming, stop on expiry/revocation, and recover by re-authenticating and reading the business snapshot before resubscribing. Do not place JWTs in URLs or treat event delivery as business state.

The materials/knowledge workflows, analysis lifecycle, and SSE subscription UI are outside 2A-0.
