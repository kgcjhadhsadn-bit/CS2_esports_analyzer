# dashboard: implementation report

**Tests:** `cd dashboard && npm ci && npm run lint && npm test && npm run build` - 64 passed, 0 failed (12 Vitest files); lint 0 warnings; `tsc --noEmit` + `vite build` clean. `.venv/bin/python -m pytest tests/foundation -q` - 37 passed.

## Summary

P10b is the owner's React dashboard in `dashboard/`: React 18, strict TypeScript, Vite 5, React Router 6, TanStack Query 5 and Recharts 2. All versions are pinned exactly and `package-lock.json` is committed. `npm run build` writes `dashboard/dist`, which `cs2bot.api` serves at http://127.0.0.1:8000. Developer docs are in `docs/DASHBOARD.md`.

An earlier engineer was cut off with almost all of the code written. I re-ran every check, compared the code with the reviewed API, ran the built app against the real API, and fixed what I found.

**Verified against the API:**
- I generated the OpenAPI schema from `cs2bot.api.create_app` and compared it programmatically with `src/api/types.ts`. Every response schema matches field for field, and all 12 enums match `cs2bot.domain.enums`.
- I parsed a real `MatchPrediction` (from `cs2bot.models.match_engine.build_match_prediction`, with players and the kill model) through `lib/analysis.ts`. Every model card rendered.

**End-to-end smoke test (ad hoc script, not in the repo):** a real uvicorn API on the API test seed (`tests/api/api_seed.py`) served the built `dist`, and headless Chromium visited every page in light and dark mode with zero console errors. It also exercised:
- the bet detail;
- kill switch on and off through the confirmation dialogs;
- saving a decision setting (the stored override shows up);
- confirming a link review;
- the login flow with auth on (wrong password, then correct, `?next=` redirect, session kept after a reload);
- rationale XSS cases: no `<script>` and no `javascript:` links reach the DOM.

**Fixes in this session:**
1. ESLint failed on irregular whitespace in two tests; it now uses ` ` / ` ` escapes.
2. Recharts printed 0x0 warnings in jsdom. The test setup now gives `ResponsiveContainer` a fixed size.
3. The kill switch label overflowed and was clipped on the Overview page. It wraps now.
4. The P&L-by-period bar chart left 0 out of the Y domain, so all-negative periods were drawn from the smallest loss.
5. Histograms had half-integer x ticks ("-13.5", "0.5"). Ticks are integers with a sensible step now (`integerTicks`).
6. Expandable rows in the markets, bets and job-run tables only worked with a mouse. A new `toggleRowProps` adds keyboard focus, Enter / Space and `aria-expanded`.
7. The markets table now says "Showing X of Y outcomes" when a filter hides rows.
8. A job's "last run" age on Health used the browser clock. It uses the server's `age_s` now.
9. The settings "Default" button became "Reset" and is disabled when the field already holds the default.
10. The bets table squeezed the match and selection columns too much; they now have minimum widths.
11. Removed references to a `cs2bot api` command, which is not in the CLI table.
12. **Security:** upgraded `react-router-dom` from 6.28.1 to 6.30.6, which fixes the high-severity open-redirect advisories. `safeNext` (the post-login `?next=` target) now also rejects backslashes, whitespace and control characters (GHSA-wrjc-x8rr-h8h6, not fixed in v6).

## Public API

The dashboard is not imported by Python code. Other packages and the owner use these interfaces.

### npm scripts (run inside `dashboard/`, Node >= 20)

| Command | Effect |
|---|---|
| `npm ci` | install exactly from `package-lock.json` |
| `npm run build` | `tsc --noEmit -p tsconfig.json && vite build` -> `dashboard/dist/` (index.html + hashed assets; pages are lazy chunks) |
| `npm run lint` | `eslint . --max-warnings 0` |
| `npm test` | `vitest run` (jsdom, no network) |
| `npm run dev` | dev server on http://localhost:5173, proxies `/api` to `CS2BOT_API_URL` (default `http://127.0.0.1:8000`; env var or `dashboard/.env.local`) |
| `npm run typecheck` / `npm run preview` | typecheck only / serve dist on :4173 |

### Wiring

- **Serving.** `cs2bot.api.create_app(settings, ...)` serves `settings.api.dashboard_dist_dir` (default `dashboard/dist`) with an SPA fallback when `settings.api.serve_dashboard` is true. No dashboard-specific setting is needed. Rebuild, then restart the API (`cs2bot up`).
- **Install / CI.** Ops scripts and CI should run `npm ci && npm run build` (CI: also `npm run lint && npm test`) in `dashboard/`. This needs only Node and no network at runtime. Example for `deploy/windows/install.ps1`:
  ```powershell
  Push-Location dashboard; npm ci; npm run build; Pop-Location
  ```
- **Client routes** (all deep-linkable): `/`, `/matches`, `/matches/:id`, `/bets`, `/performance`, `/review`, `/settings`, `/health`, `/login`.

### What the dashboard relies on from the API

These names must stay stable:
- **Endpoints:** every endpoint in docs/API.md except `GET /api/decisions` (typed and wrapped, not used by a page yet) and `GET /api/teams/{id}` (typed, not rendered yet).
- **Request rules:** every request sends `X-Requested-With: cs2bot` and `credentials: "include"`. A 401 shows `/login?next=<path>`.
- **Alert codes with inline actions:** `betclic_login_required` (Open Betclic login -> `POST /api/control/betclic-login`) and `link_review_pending` (link to `/review`).
- **Types:** `src/api/types.ts` mirrors `cs2bot/api/schemas.py`. A field added there should be added here too (TypeScript will not see it otherwise).

### Reusable front-end modules (for later dashboard work)

- `src/api/client.ts`:
  - `api.<endpoint>(...)`, `apiRequest<T>(method, path, {query, body, signal})`
  - `ApiError {status, detail, validationErrors}`
  - `buildQuery(params)`, `onUnauthorized(listener)`
- `src/api/hooks.ts`: `use<Endpoint>()` query hooks, mutations (`useKillSwitch`, `usePause`, `useResume`, `useSaveConfig`, `useStartBetclicLogin`, `useReviewAction`), and query keys `qk`.
- `src/lib/format.ts`:
  - `formatMoney(v, {signed})` (pl-PL PLN), `formatOdds`, `formatPct`, `formatSignedPct`
  - `formatDateTime` / `formatDate` / `formatTime` (Europe/Warsaw), `formatAge(seconds)`, `formatRelative`
  - `warsawDateToUtcIso(date, endOfDay)`, `teamName`, `specKeyLabel`
- `src/lib/markdown.tsx`: `SafeMarkdown({text})`, `safeHref(url)`. This is the only way untrusted text (the Claude rationale) is rendered; `dangerouslySetInnerHTML` is banned by lint.
- `src/lib/analysis.ts`: `parseAnalysis(payload) -> AnalysisView | null` (defensive `MatchPrediction` parser), `roundTotalBars`, `roundMarginBars`, `pmfStats`, `probOver`.
- `src/test/utils.tsx`: `mockApi({"GET /api/health": body | {status, body} | handler})` returns `{calls, fetchMock}`; `renderWithProviders(ui, {route})`.

## Known limitations

- **Tier filter.** `GET /api/matches` has no `tier` parameter, so the Matches page filters tier on the loaded page only; the pager still counts all server-side matches.
- **Clock.** Relative times ("in 2 h", "3 min ago") use the browser clock, except job ages on Health, which use the server's `age_s`. On one PC the two clocks are the same.
- **React Router 6.30.6** still has two moderate advisories, fixed only in v7:
  - backslash open redirect in `<Link>` / `useNavigate`: mitigated, since the only untrusted navigation target is `?next=`, which `safeNext` sanitises;
  - SSR `deserializeErrors`: does not apply, the app is client-only.
  Moving to v7 would be a major upgrade.
- **Dev-only audit findings.** The remaining `npm audit` findings (esbuild dev server, vitest, eslint plugin-kit, minimatch) are in dev tooling, not in the shipped bundle.
- **No browser test in the repo.** The end-to-end smoke run (real API + Chromium) was an ad-hoc script. The repo's dashboard tests are Vitest / jsdom with a mocked API. A Playwright test belongs to the integration package if wanted (it would need `npm run build` first).
- **Unused endpoints.** `GET /api/decisions` (decision history) and `GET /api/teams/{id}` (team page) are typed and wrapped but have no page. Market decisions and reasons are shown from the match payload.
- **Screenshots.** The screenshot preview shows "could not be loaded" when the stored file is not a valid image (the API test seed uses fake PNG bytes). Real executor screenshots are plain PNGs.
- **Bundle size.** Recharts is about 420 kB minified (113 kB gzip) in its own chunk, which only chart pages load. This is fine on localhost.

## Foundation change requests

- (api package, optional) Add a `tier` query parameter (repeatable) to `GET /api/matches`, so the tier filter can work server-side with correct paging.
- (ops / integration) `deploy/windows/install.ps1`, `update.ps1` and CI should run `npm ci && npm run build` in `dashboard/` (CI: plus `npm run lint && npm test`), as the ops brief already plans. The API serves whatever is in `dashboard/dist` when it starts.

## New dependency requests

- None for Python. npm: `react-router-dom` bumped from 6.28.1 to 6.30.6 (security fix, same major version), pinned in `dashboard/package.json` / `package-lock.json`.
