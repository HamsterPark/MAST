import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";
import { router } from "./router";
import { api } from "./api/client";
import { langTag, tr } from "./lib/i18n";
import "./index.css";

document.documentElement.lang = langTag();
document.title = tr("MAST — 实验室控制台", "MAST — Lab Console");

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false } },
});

async function boot(): Promise<void> {
  // Dev-only live data mock (/ters?livemock=1). DEV is false in production builds, so
  // this branch and the devMock module are tree-shaken out of the bundle.
  if (import.meta.env.DEV) {
    const { installLiveDevMockIfRequested } = await import("./lib/live/devMock");
    installLiveDevMockIfRequested(api);
    // Optical layout page without a backend (/experimental/layout?layoutmock=1), same rule.
    const { installLayoutDevMockIfRequested } = await import("./lib/opticalLayout/devMock");
    installLayoutDevMockIfRequested();
  }
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </React.StrictMode>,
  );
}

void boot();
