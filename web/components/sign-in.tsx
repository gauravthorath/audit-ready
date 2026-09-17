"use client";

/**
 * The sign-in panel, shown only when the API asks for a credential (Step 40).
 *
 * It is not a route guard and not a login page. The deployment decides whether
 * it needs a token (`AUDITREADY_AUTH`), so the UI waits to be told: any 401
 * from `lib/api.ts` raises the panel, and a token accepted by the next call
 * takes it away. On an open deployment nobody ever sees it.
 *
 * After a token is stored the page is reloaded rather than each screen being
 * asked to refetch. Every page loads its data on mount; a reload is one line
 * and cannot leave half the screen showing the signed-out state.
 */

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { api, type Health } from "@/lib/api";
import { clearToken, getToken, setToken, subscribeUnauthorized } from "@/lib/auth";

export function SignIn() {
  const [needed, setNeeded] = useState(false);
  const [mode, setMode] = useState<Health["auth"] | null>(null);
  const [value, setValue] = useState("");
  const [rejected, setRejected] = useState(false);

  useEffect(
    () =>
      subscribeUnauthorized(() => {
        setNeeded(true);
        setRejected(getToken() !== null); // we had one and it was refused
      }),
    [],
  );

  useEffect(() => {
    if (!needed) return;
    // `/health` is public in every mode, so it can always say which credential
    // this deployment wants — that is the difference between "sign in" and
    // "paste the token your administrator gave you".
    let cancelled = false;
    api
      .health()
      .then((h) => {
        if (!cancelled) setMode(h.auth);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [needed]);

  if (!needed) return null;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!value.trim()) return;
    setToken(value);
    window.location.reload();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="signin-title"
    >
      <form onSubmit={submit} className="card w-full max-w-md px-6 py-6">
        <div id="signin-title" className="text-[17px] font-semibold">
          This deployment needs a credential
        </div>
        <p className="mt-2 text-[13px] text-dim">
          {mode === "oidc"
            ? "Paste the access token from your organisation's identity provider. AuditReady verifies it; it never issues one."
            : "Paste the API token your administrator gave you. It is kept for this browser tab only."}
        </p>
        {rejected && (
          <p className="mt-2 text-[13px] text-red">
            The token in this tab was refused. It may have been revoked or mistyped.
          </p>
        )}
        <label className="mt-4 block text-[12px] text-dim" htmlFor="signin-token">
          {mode === "oidc" ? "Access token (JWT)" : "API token"}
        </label>
        <input
          id="signin-token"
          type="password"
          autoFocus
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="mono mt-1 w-full rounded border border-line bg-paper px-3 py-2 text-[13px] outline-none focus:border-ink"
          placeholder={mode === "oidc" ? "eyJhbGciOi…" : "••••••••"}
        />
        <div className="mt-4 flex items-center justify-between gap-3">
          <button
            type="button"
            className="text-[12.5px] text-dim underline underline-offset-2"
            onClick={() => {
              clearToken();
              window.location.reload();
            }}
          >
            Forget the stored token
          </button>
          <Button type="submit" disabled={!value.trim()}>
            Continue
          </Button>
        </div>
      </form>
    </div>
  );
}
