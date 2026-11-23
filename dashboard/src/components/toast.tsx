/**
 * Toast notifications. A module-level store so non-React code (the QueryClient's global error
 * handlers) can raise toasts; <Toaster/> renders them. Identical messages are de-duplicated while
 * visible so a failing background refetch does not stack up the same toast.
 */
import { useSyncExternalStore } from "react";

export type ToastKind = "info" | "success" | "warning" | "error";

export interface Toast {
  id: number;
  kind: ToastKind;
  title?: string;
  message: string;
}

type Listener = () => void;

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<Listener>();
const timers = new Map<number, ReturnType<typeof setTimeout>>();

function emit() {
  listeners.forEach((l) => l());
}

export function dismissToast(id: number): void {
  const timer = timers.get(id);
  if (timer) clearTimeout(timer);
  timers.delete(id);
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

export function pushToast(kind: ToastKind, message: string, title?: string, timeoutMs?: number): number {
  const existing = toasts.find((t) => t.kind === kind && t.message === message && t.title === title);
  if (existing) return existing.id;
  const id = nextId++;
  toasts = [...toasts, { id, kind, message, title }].slice(-5);
  emit();
  const ms = timeoutMs ?? (kind === "error" ? 9000 : 5000);
  if (ms > 0) timers.set(id, setTimeout(() => dismissToast(id), ms));
  return id;
}

export const toast = {
  info: (message: string, title?: string) => pushToast("info", message, title),
  success: (message: string, title?: string) => pushToast("success", message, title),
  warning: (message: string, title?: string) => pushToast("warning", message, title),
  error: (message: string, title?: string) => pushToast("error", message, title),
};

/** Test helper: remove every toast. */
export function clearToasts(): void {
  timers.forEach((t) => clearTimeout(t));
  timers.clear();
  toasts = [];
  emit();
}

function subscribe(listener: Listener) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(subscribe, () => toasts, () => toasts);
}

export function Toaster() {
  const items = useToasts();
  return (
    <div className="toasts" aria-live="polite" aria-relevant="additions">
      {items.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`} role={t.kind === "error" ? "alert" : "status"}>
          <div className="toast-body">
            {t.title && <div className="toast-title">{t.title}</div>}
            <div>{t.message}</div>
          </div>
          <button className="btn btn-ghost btn-sm" aria-label="Dismiss notification" onClick={() => dismissToast(t.id)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
