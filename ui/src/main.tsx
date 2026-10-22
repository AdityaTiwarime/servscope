import "./styles.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";

import { AdminProvider } from "./components/Admin";
import { Layout } from "./components/Layout";
import { Empty } from "./components/States";
import { Alerts } from "./pages/Alerts";
import { IncidentDetail } from "./pages/IncidentDetail";
import { Logs } from "./pages/Logs";
import { Overview } from "./pages/Overview";
import { Scenarios } from "./pages/Scenarios";
import { Services } from "./pages/Services";
import { Settings } from "./pages/Settings";
import { TraceDetail } from "./pages/TraceDetail";
import { Traces } from "./pages/Traces";

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        retry: 1,
        refetchOnWindowFocus: true,
      },
    },
  });
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="services" element={<Services />} />
        <Route path="traces" element={<Traces />} />
        <Route path="traces/:traceId" element={<TraceDetail />} />
        <Route path="logs" element={<Logs />} />
        <Route path="alerts" element={<Alerts />} />
        <Route path="alerts/incidents/:incidentId" element={<IncidentDetail />} />
        <Route path="scenarios" element={<Scenarios />} />
        <Route path="settings" element={<Settings />} />
        <Route
          path="*"
          element={<div className="panel"><Empty title="Page not found"><Link to="/">Back to the overview</Link></Empty></div>}
        />
      </Route>
    </Routes>
  );
}

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={makeQueryClient()}>
        <BrowserRouter>
          <AdminProvider>
            <AppRoutes />
          </AdminProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </StrictMode>,
  );
}
