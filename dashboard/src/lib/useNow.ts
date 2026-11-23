import { useEffect, useState } from "react";

function roundDown(stepMs: number): Date {
  return new Date(Math.floor(Date.now() / stepMs) * stepMs);
}

/**
 * Current time rounded down to `stepMs`, refreshed at most once a minute. Use it for time-based
 * query parameters ("matches from now") so the query key stays stable between renders.
 */
export function useNow(stepMs = 60_000): Date {
  const [now, setNow] = useState(() => roundDown(stepMs));
  useEffect(() => {
    const id = setInterval(() => {
      const next = roundDown(stepMs);
      setNow((prev) => (prev.getTime() === next.getTime() ? prev : next));
    }, Math.min(stepMs, 60_000));
    return () => clearInterval(id);
  }, [stepMs]);
  return now;
}
