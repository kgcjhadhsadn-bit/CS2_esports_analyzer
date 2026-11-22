# P10b - React dashboard (dashboard/)

> Work-package brief: the requirements this package was built against. Keep it in sync when scope changes.

**Owned paths:** dashboard/ (the entire directory; create it), docs/DASHBOARD.md

**Tests:** dashboard/ (Vitest)

## Requirements

Goal: a polished, fast, local web dashboard (served by the FastAPI app from dashboard/dist at http://127.0.0.1:8000) for one user: the owner. English UI; money formatted as Polish zloty with Intl.NumberFormat("pl-PL", {style: "currency", currency: "PLN"}); times shown in Europe/Warsaw.

Stack: React 18 + TypeScript (strict) + Vite + React Router + TanStack Query + Recharts. No heavyweight UI kit; write a small, clean design system (CSS variables, light/dark via prefers-color-scheme, accessible contrast, responsive down to laptop width). The npm registry is reachable; pin exact versions in package.json and commit package-lock.json.

Read docs/API.md and src/cs2bot/api/ (implemented just before you in this chain) and type every response in src/api/types.ts. API client: fetch with credentials "include", JSON, and the header "X-Requested-With: cs2bot" on every mutating request; 401 -> login page (when auth is enabled); errors surfaced as toasts.

Pages:
1. Overview: balance, open exposure, today's/this week's P&L with loss-limit usage meters, dry-run indicator, a BIG kill switch and pause/resume controls (confirmation dialogs), alerts (e.g. "Betclic login required" with a button calling POST /api/control/betclic-login), next matches with accepted bets, recent bets.
2. Matches: upcoming/past table with filters (status, linked, tier), data completeness and best-EV badges.
3. Match detail: teams + lineups, veto (known steps, or the predicted map distribution per slot as bars), per-map win probabilities with uncertainty, round-total and round-handicap distributions (histograms from the analysis payload), overtime/pistol probabilities, player kill projections, a markets table (model p vs Betclic odds, EV after tax, sigma, decision status with reasons on expand), odds history chart per selection, context flags, the Claude rationale rendered SAFELY (no dangerouslySetInnerHTML; a minimal safe Markdown renderer or plain text with paragraphs/bullets).
4. Bets: history with filters and status badges, P&L, CLV, screenshot preview (/api/bets/{id}/screenshot).
5. Performance: equity curve, P&L by period, ROI/hit rate/CLV by market type, calibration reliability diagram (with the diagonal), drawdown.
6. Review: pending Betclic event links with candidate matches; confirm/reject/relink actions.
7. Settings: decision thresholds and bankroll caps editor (validated forms showing effective values and defaults; PUT /api/control/config), kill switch, dry-run status, Betclic login button.
8. Health: job runs with ages/status colours, data freshness, executor health.

Quality: npm run build (tsc --noEmit + vite build) passes with zero TypeScript errors; ESLint (typescript-eslint + react-hooks) with npm run lint passing; a few Vitest + Testing Library tests for formatting helpers and one page with a mocked API (npm test passes). The Vite dev server proxies /api to http://127.0.0.1:8000. Document in docs/DASHBOARD.md (dev workflow, build, structure). Never commit node_modules or dist (already ignored). DEFINITION OF DONE for you: npm ci, npm run lint, npm test and npm run build all pass inside dashboard/ (instead of the Python checks), and the Python foundation tests still pass.

## Addendum (after the first build wave)

- Build against the REVIEWED API (docs/API.md and docs/dev/reports/api.md).
