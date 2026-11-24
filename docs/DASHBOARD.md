# Dashboard (`dashboard/`)

The owner's local web dashboard: React 18 + TypeScript (strict) + Vite + React Router +
TanStack Query + Recharts. It is a static single-page app. The FastAPI app (`cs2bot.api`) serves the
built files from `dashboard/dist` at **http://127.0.0.1:8000**, and the app reads every number from
the local JSON API under `/api` (see [API.md](API.md)). The dashboard has no server-side state.

- English UI. Money is shown as Polish zloty (`Intl.NumberFormat("pl-PL", {style: "currency",
  currency: "PLN"})`). Every time is shown in **Europe/Warsaw**. The API sends UTC ISO strings and
  the dashboard converts them.
- Light and dark themes follow `prefers-color-scheme`. Layout works from laptop width upwards; the
  sidebar collapses to icons below 900 px.
- One user, no UI kit: a small design system in `src/styles/global.css` (CSS variables) and
  `src/components/ui.tsx`.

## Pages

| Route | Page | Main endpoints |
|---|---|---|
| `/` | **Overview**: balance, open exposure, today's / this week's P&L with loss-limit meters, dry-run indicator, a big kill switch, pause / resume (confirmation dialogs), alerts (with "Open Betclic login" on `betclic_login_required` and "Review" on `link_review_pending`), next matches (filter "only with accepted bets"), recent bets. The real money / paper toggle defaults to the API's dry-run mode. | `/api/health`, `/api/bankroll`, `/api/control/*`, `/api/matches`, `/api/bets` |
| `/matches` | **Matches**: upcoming / past / all, filters for status, Betclic link, Tier 1/2 scope and tier, plus data-completeness and best-EV badges. The tier filter applies to the loaded page only, because the API has no tier parameter. | `/api/matches` |
| `/matches/:id` | **Match detail**: teams, HLTV ranks, status and link badges. The **Markets** tab: model p (P(win \| not refunded)), sigma, fair and break-even odds, Betclic odds (stale / suspended badges), EV after tax, the decision with its reasons when a row is expanded, the bet, and the odds history chart for each market (step line; a gap means the selection was suspended). The **Model** tab: win probabilities with -1σ / +1σ brackets per match and map slot, map-score distribution, per-map estimates, veto (published steps, otherwise the predicted map distribution for each slot plus the most likely sequences), lineups with stand-ins, maps played, round-total and round-margin histograms per slot, overtime / pistol / half probabilities, and player kill projections for each scope (mean, sd, 80 % range). The **Context** tab: the Claude rationale (safe Markdown) and context flags with evidence links. | `/api/matches/{id}`, `/api/matches/{id}/odds-history` |
| `/bets` | **Bets**: real / paper / all, status and result chips, Warsaw date range, a summary (stake, P&L, W/L/V, hit rate), status badges, EV, closing odds, CLV and P&L. Expanding a row (click or Enter) shows the Betclic texts, the decision with reasons and settings snapshot, ledger rows, the rationale and the placement screenshot. | `/api/bets`, `/api/bets/{id}`, `/api/bets/{id}/screenshot` |
| `/performance` | **Performance**: real / paper, KPIs (P&L, ROI, yield, hit rate, CLV, max drawdown), equity curve, drawdown, P&L by day / week / month, a table by market type (ROI, hit rate, CLV, average EV), the calibration reliability diagram (dashed diagonal, 95 % Wilson intervals, point size = sample count) for all predictions, per market or for bets, and the Brier score by month. | `/api/bankroll`, `/api/performance/markets`, `/api/performance/calibration` |
| `/review` | **Link review**: Betclic events waiting for review, rejected, confirmed or auto-linked, with candidate matches (score, name score, time difference, suggested orientation). Actions: Confirm, Link here, Relink by match id (with "teams swapped") and Reject, each behind a confirmation. | `/api/review/links` |
| `/settings` | **Settings**: decision thresholds and bankroll caps editor with client-side validation that mirrors the API ranges, the new / effective / default value of every field, per-market threshold overrides, disabled markets, and reset to defaults (PUT `/api/control/config`; server 422s are shown next to the fields). Also the kill switch, pause, dry-run status and the Betclic login button. | `/api/control/config`, `/api/control/*`, `/api/health` |
| `/health` | **Health**: overall status, alerts, executor health (selectors, login, balance), data freshness ages, job health (status colours, stale / failing, interval, next due), the built-in scheduler, and the recent job-run log with filters. | `/api/health`, `/api/jobs` |
| `/login` | Login form, shown only when the API has auth on (`api.password` set). | `/api/auth/*` |

Data refreshes by polling: health and trading state every 15 s, bankroll and bets every 30 s,
matches every 60 s, and the Betclic login status every 2 s while a login window is open. Data is
also refetched when the window regains focus.

## API client rules (`src/api/client.ts`)

- `fetch` with `credentials: "include"` and JSON bodies. Every request sends
  `X-Requested-With: cs2bot`, which the API requires on POST / PUT / PATCH / DELETE (CSRF check).
- A non-2xx answer becomes an `ApiError(status, message, detail)`. FastAPI 422 lists become
  readable `field: message` text.
- On a 401, the auth provider (`src/auth.tsx`) switches to the login page and keeps the current path
  in `?next=` (only same-app paths are accepted).
- Errors are shown as toasts (`src/api/queryClient.ts`). 4xx answers are not retried. The settings
  page shows 422s inline.
- Every response is typed in `src/api/types.ts`. These types mirror `src/cs2bot/api/schemas.py`
  field for field, and every enum in `cs2bot.domain.enums`. The analysis payload
  (`MatchPrediction`) is parsed defensively in `src/lib/analysis.ts`, because it is passed through
  as JSON and may come from older model versions.

## Security notes

- The Claude rationale and every context-flag text are untrusted. They are rendered by a minimal
  Markdown renderer (`src/lib/markdown.tsx`: headings, paragraphs, bullet / numbered lists, bold,
  italic, code, links) that only produces React text nodes. `dangerouslySetInnerHTML` is banned by
  an ESLint rule. Links are rendered only for `https:` / `http:` URLs (`safeHref`) with
  `rel="noopener noreferrer"`; anything else stays as plain text.
- The dashboard never stores credentials. The session is an HttpOnly cookie set by the API.

## Development workflow

Requires Node 20+ (developed with Node 22). Run all commands inside `dashboard/`:

```bash
npm ci            # install exactly what package-lock.json pins
npm run dev       # Vite dev server on http://localhost:5173, proxies /api to http://127.0.0.1:8000
npm run lint      # ESLint (typescript-eslint + react-hooks), zero warnings allowed
npm test          # Vitest + Testing Library (jsdom), no network
npm run build     # tsc --noEmit + vite build -> dashboard/dist
npm run preview   # serve the built dist on http://localhost:4173 (no /api proxy)
```

- Start the backend first (`cs2bot up`). The API's defaults
  already allow the dev server: `http://localhost:5173` is in `api.cors_origins`, and the proxy
  keeps the `localhost:5173` Host header, which the API's loopback allow-list accepts.
- To proxy to an API somewhere else, set `CS2BOT_API_URL` (environment variable or
  `dashboard/.env.local`), e.g. `CS2BOT_API_URL=http://127.0.0.1:8001 npm run dev`.
- Production: run `npm run build`, then (re)start the API. The API serves `dashboard/dist` when
  `api.serve_dashboard` is true (the default) and `dist/index.html` exists, with an SPA fallback for
  deep links such as `/matches/42`. Without a build, `/` returns a JSON hint. The Windows install
  scripts run the build.
- `node_modules/` and `dist/` are git-ignored. `package.json` pins exact versions
  (`.npmrc: save-exact=true`), and `package-lock.json` is committed.

## Structure

```
dashboard/
  index.html, vite.config.ts (dev proxy, chunks, Vitest config), tsconfig.json (strict),
  eslint.config.js, package.json / package-lock.json, public/favicon.svg
  src/
    main.tsx              entry: QueryClient + App
    App.tsx               routes (pages are lazy-loaded chunks), auth gate, toaster
    auth.tsx              /api/auth/me gate, 401 watcher
    api/
      types.ts            response / request types (mirror of cs2bot.api.schemas)
      client.ts           fetch wrapper, ApiError, `api.*` endpoint functions
      hooks.ts            TanStack Query hooks, query keys, mutations (+ cache invalidation)
      queryClient.ts      error toasts, retry policy
    pages/                one file per page (+ *.test.tsx)
    components/
      ui.tsx              Card, Stat, Badge, Meter, ProbBar, HBar, Segmented, Chips, Tabs, Pager,
                          ConfirmDialog, QueryView, Loading / Empty / ErrorState, toggleRowProps
      badges.tsx          status / link / tier / EV / completeness / age badges
      tables.tsx          matches and bets tables
      charts.tsx          theme-aware chart colours, Histogram, tooltip, integer ticks
      TradingControls.tsx kill switch, pause / resume, Betclic login button
      AlertList.tsx       health alerts with inline actions
      Layout.tsx          sidebar navigation (review count, health dot, dry-run badge)
      toast.tsx           toast store + Toaster
      match/              MarketsTable, OddsHistoryChart, AnalysisSections (model cards)
    lib/
      format.ts           money (pl-PL / PLN), odds, percentages, Warsaw dates, ages, labels
      analysis.ts         defensive parser of the MatchPrediction payload, pmf helpers
      markdown.tsx        safe Markdown renderer, safeHref
      settingsSchema.ts   settings form specs, validation, PUT body builder
      useNow.ts           ticking "now" for relative times
    styles/global.css     design tokens (light / dark), layout, components
    test/                 setup (jsdom shims, fixed-size charts), fixtures, mockApi helper
```

### Tests

Vitest runs in jsdom. `src/test/utils.tsx` provides `mockApi({"GET /api/health": body, ...})`
(a routed `fetch` stub that records calls, including headers and credentials) and
`renderWithProviders(ui, {route})`. The fixtures in `src/test/fixtures.ts` follow the shapes in
docs/API.md. The tests cover the formatting helpers, the analysis parser, the Markdown renderer (XSS
cases), the settings validation, the API client (CSRF header, credentials, 401 / 422 handling), the
app's auth flow, and the Overview, Matches, Match detail, Review and Settings pages against the
mocked API.

### Adding an endpoint or a field

1. Add or adjust the type in `src/api/types.ts`. It must match `src/cs2bot/api/schemas.py`.
2. Add the function to `api` in `client.ts` and a hook in `hooks.ts`, with a query key under the
   endpoint's group so mutations can invalidate it.
3. Render it with `QueryView` (it handles loading, error and retry) and the formatting helpers in
   `lib/format.ts`. Never format money or dates by hand.
