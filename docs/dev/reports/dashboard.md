# dashboard: implementation report

**Tests:** `cd dashboard && npm ci && npm run lint && npm test && npm run build` - 79 passed, 0 failed (14 Vitest files; 64 before review round 1); lint 0 warnings; `tsc --noEmit` + `vite build` clean. `.venv/bin/python -m pytest tests/foundation -q` - 37 passed.

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
- `src/api/hooks.ts`: `use<Endpoint>()` query hooks, mutations (`useKillSwitch`, `usePause`, `useResume`, `useSaveConfig`, `useStartBetclicLogin`, `useReviewAction`), query keys `qk`, and `useMinDataCompleteness()` (effective `decision.min_data_completeness` from the shared config query; `DEFAULT_MIN_DATA_COMPLETENESS` = 0.95 until it loads).
- `src/lib/format.ts`:
  - `formatMoney(v, {signed})` (pl-PL PLN), `formatOdds`, `formatPct`, `formatSignedPct`
  - `formatDateTime` / `formatDate` / `formatTime` (Europe/Warsaw), `formatAge(seconds)`, `formatRelative`
  - `warsawDateToUtcIso(date, endOfDay)`, `teamName`, `specKeyLabel`
- `src/lib/markdown.tsx`: `SafeMarkdown({text})`, `safeHref(url)`. This is the only way untrusted text (the Claude rationale) is rendered; `dangerouslySetInnerHTML` is banned by lint.
- `src/lib/analysis.ts`: `parseAnalysis(payload) -> AnalysisView | null` (defensive `MatchPrediction` parser), `roundTotalBars`, `roundMarginBars`, `pmfStats`, `probOver`, `fillIntegerGaps(bars)` (zero bars in integer gaps, a lone value padded; used by `Histogram`).
- `src/components/match/OddsHistoryChart.tsx`: `groupOddsSeries(series)`, `mergeSeries(series, until?)` (step series on one time axis; `until` carries the last prices forward to that instant), `<OddsHistoryChart groups selectedKey onSelect until? />`.
- `src/pages/ReviewPage.tsx`: `candidateAction(candidate)`, `manualRelinkBody(matchId, "auto" | "same" | "swapped")` ("auto" omits `teams_swapped` so the API detects the order).
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

## Review round 1

Independent review against `docs/dev/briefs/dashboard.md` (incl. the Addendum: build against the reviewed API). I read every source file, re-ran `npm ci`, lint, tests and build, and repeated the end-to-end smoke run (ad-hoc script, not in the repo): a real uvicorn API on the API test seed (`tests/api/api_seed.py`) serving the rebuilt `dist`, headless Chromium (`/opt/pw-browsers/chromium`) visiting every page in light and dark mode, engaging and releasing the kill switch through the dialogs - zero console errors.

**Verified (no change needed):**
- `src/api/types.ts` against the OpenAPI schema generated from `cs2bot.api.create_app` (field names and nullability of every response schema, programmatic comparison): all match; the API has not changed since the dashboard was built. The analysis payload types and `parseAnalysis` match `cs2bot.domain.predictions.MatchPrediction` (team1-first score keys, slot distributions conditional on the map being played, pistol keys "1"/"13", half keys "1"/"2", low/high = -1/+1 sigma scenarios).
- Client rules: `credentials: "include"`, `X-Requested-With: cs2bot` on every request, 401 -> login with a sanitised `?next=`, errors as toasts, 4xx not retried.
- Math shown to the owner: round-total / round-margin histograms from the joint score pmf, handicap cover probabilities (team1 -4.5 covers = P(margin > 4.5), +4.5 = P(margin > -4.5)), Wilson error bars, drawdown below a running peak starting at 0, P&L bars from 0, Warsaw date bounds across both DST switches, EV/tax labels, model p = P(win | not refunded).
- Settings editor ranges mirror `src/cs2bot/api/configedit.py`; a PUT resends every non-default field of the section (the API replaces the section's overrides).
- Untrusted text: the rationale goes through `SafeMarkdown` only, flag rationales / errors / toasts are text nodes, links only for http(s) (`safeHref`), `dangerouslySetInnerHTML` banned by lint.

**Issues found and fixed:**
1. **(major, money safety) Kill switch dialog could send the opposite action.** The dialog derived its action from the live trading state, which polls every 15 s. If the switch changed while the "Engage the kill switch?" dialog was open (engine or another tab), the dialog silently turned into "Release kill switch" under the cursor and confirming released it. The action is now fixed when the dialog opens (`target`); confirming always sends what the dialog said. Test: `OverviewPage.test.tsx` "keeps the dialog's action when the switch changes underneath it".
2. **(major, money safety) Manual relink always sent `teams_swapped: false`.** The review page's "Relink to match id" form had an unchecked "teams swapped" checkbox whose value was always sent, overriding the API's orientation detection from the team names. Relinking to a match that Betclic lists the other way round stored the wrong orientation, so selections would be keyed to the wrong team. The form now has a "Team order" select defaulting to "detect from names" (omits `teams_swapped`), with explicit "same" / "swapped" options; the confirmation names the choice. Tests: `ReviewPage.test.tsx` (request bodies) and `manualRelinkBody`.
3. **(major) Log out did nothing visible.** `logout` cleared the whole query cache (`qc.clear()`), removing the `/auth/me` query from under its mounted observer, then invalidated a query that no longer existed: nothing re-rendered and the owner stayed on the page. It now sets the logged-out state on the `/auth/me` query and removes only the other cached data; a failed logout shows a toast instead of pretending. Test: `App.test.tsx` "shows the login page right after logging out".
4. **(minor) Histograms with a single value drew no bar; sparse pmfs drew multi-unit bars.** Recharts sizes bars on a numeric axis by the smallest gap between data points. `fillIntegerGaps` adds zero bars in integer gaps and pads a lone value (seen in the smoke run: a one-score map pmf rendered an empty chart). Tests in `analysis.test.ts`.
5. **(minor) Odds history ended at the last price change.** A selection whose price never changed (one snapshot) drew nothing, and every series stopped at the last change. `mergeSeries(series, until)` now carries the last prices forward to min(now, match start); the history also polls every 60 s like the match. Tests: `OddsHistoryChart.test.ts`.
6. **(minor) Data-completeness badges used a hard-coded 0.95.** They now use the effective `decision.min_data_completeness` (dashboard overrides included) via `useMinDataCompleteness`, so green means "passes the engine's check". Test: `MatchDetailPage.test.tsx`.
7. **(minor, a11y) Player kill projection rows were mouse-only** (missed by the earlier `toggleRowProps` fix). Now focusable with Enter / Space and `aria-expanded`. Test: `MatchDetailPage.test.tsx`.
8. **(minor) Performance page defaulted to real money** even in dry-run (the default mode), showing an empty book; it now defaults to the API's mode like the overview. Test: `PerformancePage.test.tsx` (also covers `drawdownSeries` and `reliabilityPoints`, previously untested). The drawdown chart's empty state no longer says "No settled bets yet" when there is exactly one.
9. `docs/DASHBOARD.md` updated (relink team order, polling, completeness threshold, performance default).

**Remaining concerns (not fixed):**
- The tier filter on the Matches page still filters the loaded page only (the API has no `tier` parameter; see the foundation change requests).
- The `/api/control/config` request made for the completeness threshold toasts on failure like any query; when the API is down every page already shows errors, so this adds at most one more de-duplicated toast.
- The Betclic login status is polled only while a window is open; the `betclic_login_required` alert disappears on the next health poll (<= 15 s) after a successful login, not immediately.
- React Router 6 advisories and dev-only `npm audit` findings as listed under Known limitations.
