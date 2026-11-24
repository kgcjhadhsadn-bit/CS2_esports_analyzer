import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { cloneElement, createElement } from "react";
import type { ReactElement } from "react";
import type * as Recharts from "recharts";
import { afterEach, vi } from "vitest";
import { clearToasts } from "../components/toast";

// Recharts' ResponsiveContainer needs ResizeObserver, which jsdom does not implement.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
if (!("ResizeObserver" in globalThis)) {
  (globalThis as unknown as { ResizeObserver: typeof ResizeObserverStub }).ResizeObserver = ResizeObserverStub;
}
if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList;
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => undefined;
}

// jsdom has no layout, so ResponsiveContainer would measure 0 x 0 and warn on every chart: in tests
// it is replaced by a fixed 800 x 300 box (the chart inside still renders its SVG).
vi.mock("recharts", async (importOriginal) => {
  const mod = await importOriginal<typeof Recharts>();
  const ResponsiveContainer = ({ children }: { children: ReactElement<{ width?: number; height?: number }> }) =>
    createElement("div", { className: "recharts-responsive-container" }, cloneElement(children, { width: 800, height: 300 }));
  return { ...mod, ResponsiveContainer };
});

afterEach(() => {
  cleanup();
  clearToasts();
});
