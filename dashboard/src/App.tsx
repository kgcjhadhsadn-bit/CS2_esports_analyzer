import { QueryClientProvider } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import type { ReactNode } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { RequireAuth, UnauthorizedWatcher } from "./auth";
import { Layout } from "./components/Layout";
import { Toaster } from "./components/toast";
import { Loading } from "./components/ui";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";

// Chart-heavy pages are split into their own chunks so the first paint stays small.
const OverviewPage = lazy(() => import("./pages/OverviewPage"));
const MatchesPage = lazy(() => import("./pages/MatchesPage"));
const MatchDetailPage = lazy(() => import("./pages/MatchDetailPage"));
const BetsPage = lazy(() => import("./pages/BetsPage"));
const PerformancePage = lazy(() => import("./pages/PerformancePage"));
const ReviewPage = lazy(() => import("./pages/ReviewPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const HealthPage = lazy(() => import("./pages/HealthPage"));

/** Opt into React Router v7 behaviour now (silences the v6 deprecation warnings). */
export const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

function Page({ children }: { children: ReactNode }) {
  return <Suspense fallback={<Loading lines={6} />}>{children}</Suspense>;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<Page><OverviewPage /></Page>} />
        <Route path="matches" element={<Page><MatchesPage /></Page>} />
        <Route path="matches/:id" element={<Page><MatchDetailPage /></Page>} />
        <Route path="bets" element={<Page><BetsPage /></Page>} />
        <Route path="performance" element={<Page><PerformancePage /></Page>} />
        <Route path="review" element={<Page><ReviewPage /></Page>} />
        <Route path="settings" element={<Page><SettingsPage /></Page>} />
        <Route path="health" element={<Page><HealthPage /></Page>} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

export function App({ queryClient }: { queryClient: QueryClient }) {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter future={ROUTER_FUTURE}>
        <UnauthorizedWatcher />
        <AppRoutes />
        <Toaster />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
