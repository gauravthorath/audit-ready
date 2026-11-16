"use client";

/**
 * The caller's credential, held for as long as the tab is open (Step 40).
 *
 * `sessionStorage`, not `localStorage`: a token that survives closing the tab
 * is a token left on a shared machine. It survives a reload, which is all the
 * app needs, and it is per-tab, so signing in to one deployment in one tab
 * does not sign you in somewhere else in another.
 *
 * Nothing here decides *whether* a credential is needed - `GET /health` says
 * which mode the deployment runs, and the API answers 401 when it wants one.
 * The UI reacts to that answer rather than predicting it, so the same build
 * works against an open laptop deployment and a locked-down one.
 */

const KEY = "auditready.token";
const CHANGE_EVENT = "auditready:token";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null; // private window, blocked storage - the app still runs, unauthenticated
  }
}

export function setToken(token: string): void {
  try {
    sessionStorage.setItem(KEY, token.trim());
  } catch {
    /* nothing to do: the call below still tells the app to retry */
  }
  announce();
}

export function clearToken(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
  announce();
}

function announce(): void {
  try {
    window.dispatchEvent(new Event(CHANGE_EVENT));
  } catch {
    /* not in a browser */
  }
}

export function subscribeToken(onChange: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

// -- "the API asked for a credential" ---------------------------------------
//
// `lib/api.ts` has no React in it and must not grow any, so it announces a 401
// on the window and whoever is mounted listens. One event, any number of
// listeners, no import cycle between the transport and the components.

const UNAUTHORIZED_EVENT = "auditready:unauthorized";

export function announceUnauthorized(): void {
  try {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  } catch {
    /* not in a browser */
  }
}

export function subscribeUnauthorized(onUnauthorized: () => void): () => void {
  window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
}
