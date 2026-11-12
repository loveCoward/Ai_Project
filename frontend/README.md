# BidDesk React Workspace (2A-0 / 2A-3)

React/TypeScript/Vite entry for authentication and the minimum materials, knowledge, and evidence pages that consume the presales business HTTP API. The access token stays in memory; reloading requires signing in again. No model key is needed for frontend type checking or building.

## Development

Start the configured FastAPI service on `127.0.0.1:8000`, then run:

```sh
npm install
npm run dev
```

The repository ignores `package-lock.json`, so no lockfile is committed and `npm ci` cannot run; dependency resolution follows the ranges in `package.json`. Vite proxies `/auth` and `/api` to FastAPI. Set Vite's proxy target in `vite.config.ts` when the API listens elsewhere. The configured login accounts remain server-side; never add passwords or signing secrets to this frontend.

## Current scope

- Login, identity verification, protected `/workspace` route, expiry-driven sign-out, and logout.
- Draft JSON read and material binary download through the existing protected API.
- Evidence search with explicit applicability scope and source declaration, restricted hit list, and evidence location lookup for the current reader.
- Task input status read, material parsing, per-material necessity classification, and full input manifest submission for architects with edit rights.
- Knowledge version list and detail, parsing, index build submission, candidate publish/replace/retire review submission, reviewer decisions with a mandatory reason, and applicability scope adjustment.
- File registration for task materials and knowledge candidate versions, with client-side 20 MiB rejection and a reused request id per content fingerprint so a retry does not silently register twice.
- `401` clears the in-memory identity; `404` does not distinguish missing objects from denied access.
- Restricted responses are not cached by the client. Access is re-verified against `/api/workbench` every 15 seconds, and a change of `authorization_revision` or `catalog_revision` clears restricted results and asks for a re-read.
- SSE is not implemented here. Future subscriptions must use an Authorization Bearer header, recheck current access while streaming, stop on expiry/revocation, and recover by re-authenticating and reading the business snapshot before resubscribing. Do not place JWTs in URLs or treat event delivery as business state.

## Generated contracts

`src/contracts.generated.ts` is produced from the Pydantic request/response models; do not hand-edit it and do not maintain a second set of enums:

```sh
PYTHONPATH=src uv run python scripts/export_presales_dto.py
PYTHONPATH=src uv run python scripts/export_presales_dto.py --check
```

The script imports `presales` directly, so `src` must be on `PYTHONPATH`; in Windows PowerShell set `$env:PYTHONPATH="src"` for the session instead of using the inline prefix.

## Checks

```sh
npm run typecheck
npm run build
```

## Out of scope

This frontend has no Node business server and makes no authorization or state-transition decisions of its own; it only offers actions the API already permits. The proposal analysis run page, SSE subscription UI, formal proposal approval, publish, and release-recovery pages are not implemented. There are no unit tests or browser E2E tests yet, so a passing typecheck and build does not substitute for business-flow, expiry, or privilege-escalation verification.
