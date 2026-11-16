"use client";

/**
 * A UI preference that survives reloads: view mode, collapsed panels.
 *
 * localStorage read through useSyncExternalStore, the same pattern nav.tsx
 * uses for the collapsed flag: hydration-safe (the server snapshot is the
 * default), no setState-in-effect, and other tabs follow via the native
 * `storage` event. Values are validated on read, so a stale or hand-edited key
 * falls back to the default instead of rendering an unknown mode.
 */

import { useCallback, useSyncExternalStore } from "react";

const CHANGE_EVENT = "auditready:setting";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  window.addEventListener(CHANGE_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(CHANGE_EVENT, onChange);
  };
}

export function useLocalSetting<T extends string>(
  key: string,
  fallback: T,
  allowed: readonly T[],
): [T, (v: T) => void] {
  const storageKey = `auditready.${key}`;
  const read = useCallback((): T => {
    try {
      const raw = localStorage.getItem(storageKey);
      return raw !== null && (allowed as readonly string[]).includes(raw) ? (raw as T) : fallback;
    } catch {
      return fallback;
    }
  }, [storageKey, fallback, allowed]);

  const value = useSyncExternalStore(subscribe, read, () => fallback);
  const set = useCallback(
    (v: T) => {
      try {
        localStorage.setItem(storageKey, v);
      } catch {
        /* storage unavailable - the event still re-renders this tab */
      }
      window.dispatchEvent(new Event(CHANGE_EVENT));
    },
    [storageKey],
  );
  return [value, set];
}
