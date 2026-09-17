"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, useSyncExternalStore } from "react";
import { api, type Health, type Spend } from "@/lib/api";
import { ThemeCycleButton, ThemeToggle } from "@/components/theme-toggle";
import { Logo } from "@/components/logo";

const ITEMS = [
  { href: "/", label: "Audit report", hint: "every record × every control", icon: ReportIcon },
  { href: "/chat", label: "Compliance chat", hint: "one question, cited", icon: ChatIcon },
  { href: "/records", label: "Records", hint: "the evidence under audit", icon: RecordsIcon },
];

const STORAGE_KEY = "auditready.nav.collapsed";
const CHANGE_EVENT = "auditready:nav";

// The collapsed flag lives in localStorage and is read through
// useSyncExternalStore: hydration-safe (the server snapshot is "expanded"),
// and no setState-in-effect. Other tabs follow via the native storage event.
function readCollapsed(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}
function subscribeCollapsed(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  window.addEventListener(CHANGE_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(CHANGE_EVENT, onChange);
  };
}
function writeCollapsed(value: boolean): void {
  try {
    localStorage.setItem(STORAGE_KEY, value ? "1" : "0");
  } catch {
    /* storage unavailable — the toggle still works for this render via the event */
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function Nav() {
  const path = usePathname();
  const [health, setHealth] = useState<Health | null>(null);
  const [spend, setSpend] = useState<Spend | null>(null);
  const [down, setDown] = useState(false);
  const collapsed = useSyncExternalStore(subscribeCollapsed, readCollapsed, () => false);
  const toggle = () => writeCollapsed(!collapsed);

  useEffect(() => {
    let cancelled = false;
    const probe = () => {
      api
        .health()
        .then((h) => !cancelled && (setHealth(h), setDown(false)))
        .catch(() => !cancelled && setDown(true));
      // Spend is a courtesy line; a failure here must not mark the API down.
      api
        .spend()
        .then((s) => !cancelled && setSpend(s))
        .catch(() => undefined);
    };
    probe();
    const t = setInterval(probe, 15_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  return (
    <aside
      className={`sticky top-0 flex h-screen shrink-0 flex-col border-r border-line bg-paper transition-[width] duration-200 ${
        collapsed ? "w-[64px]" : "w-[248px]"
      }`}
    >
      {/* brand + toggle */}
      <div className={`flex border-b border-line ${collapsed ? "flex-col items-center gap-2 px-2 py-4" : "items-center justify-between px-5 py-5"}`}>
        <Link href="/" className="flex items-center gap-2.5" title={collapsed ? "AuditReady" : undefined}>
          <Logo size={collapsed ? 28 : 24} />
          {!collapsed && <span className="text-[19px] font-semibold tracking-tight">AuditReady</span>}
        </Link>
        <button
          type="button"
          onClick={toggle}
          className="rounded-md p-1.5 text-dim hover:bg-neutral hover:text-ink"
          aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
          title={collapsed ? "Expand" : "Collapse"}
        >
          <ChevronIcon flipped={collapsed} />
        </button>
      </div>
      {!collapsed && (
        <p className="px-5 pt-4 text-[12.5px] leading-snug text-dim">
          Grounded, cited compliance answers. No citation, no verdict.
        </p>
      )}

      <nav className={`flex flex-col gap-1 py-4 ${collapsed ? "px-2" : "px-3"}`}>
        {ITEMS.map((it) => {
          const active = it.href === "/" ? path === "/" : path.startsWith(it.href);
          const Icon = it.icon;
          return (
            <Link
              key={it.href}
              href={it.href}
              title={collapsed ? it.label : undefined}
              className={`relative flex items-center gap-3 rounded-md transition-colors ${
                collapsed ? "justify-center py-2.5" : "py-2.5 pl-4 pr-3"
              } ${
                active
                  ? "text-ink before:absolute before:left-0 before:top-2 before:bottom-2 before:w-[3px] before:rounded-full before:bg-green"
                  : "text-body hover:bg-neutral"
              }`}
            >
              <Icon className={`h-[18px] w-[18px] shrink-0 ${active ? "text-green" : "text-dim"}`} />
              {!collapsed && (
                <span className="min-w-0">
                  <span className="block text-[14px] font-semibold">{it.label}</span>
                  <span className="block text-[12px] text-dim">{it.hint}</span>
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      {/* engine status */}
      <div className={`mt-auto border-t border-line text-[12px] text-dim ${collapsed ? "px-2 py-4" : "px-5 py-5"}`}>
        {collapsed ? (
          <div className="flex flex-col items-center gap-3">
          <ThemeCycleButton />
          <div className="flex justify-center" title={down ? "API unreachable" : health ? `${health.model} · ${health.corpus_dir} · ${health.vector_store}` : "connecting…"}>
            <span className={`inline-block h-2.5 w-2.5 rounded-full ${down ? "bg-red" : health ? "bg-green" : "bg-dim"}`} />
          </div>
          </div>
        ) : (
          <>
            <div className="eyebrow mb-2">Engine</div>
            {down ? (
              <div className="text-red">
                API unreachable.
                <div className="mono mt-1 text-[11.5px]">uv run uvicorn api.main:app --port 8000</div>
              </div>
            ) : health ? (
              <>
                <div className="mono break-all text-[11.5px] text-body">{health.model}</div>
                <div className="mt-1 truncate" title={health.corpus_dir}>corpus: {health.corpus_dir}</div>
                <div className="truncate" title={health.vector_store}>store: {health.vector_store}</div>
                {spend && (
                  <div
                    className="mt-1 truncate"
                    title={`Spent today by you: $${spend.today_usd.toFixed(4)} · by everyone: $${spend.deployment_today_usd.toFixed(4)} · limits: ${spend.limits.chat_per_minute} chat turns/min, ${spend.limits.audits_per_hour} audits/hour, ${spend.limits.max_pairs_per_run} pairs/run (${spend.basis})`}
                  >
                    spend today: <span className="mono">${spend.deployment_today_usd.toFixed(4)}</span>
                    {spend.remaining_usd !== null && spend.remaining_usd !== undefined ? (
                      <> · <span className="mono">${spend.remaining_usd.toFixed(2)}</span> left</>
                    ) : (
                      <> · no cap</>
                    )}
                  </div>
                )}
              </>
            ) : (
              <div>connecting…</div>
            )}
            <div className="mt-4 flex items-center justify-between gap-2">
              <span className="eyebrow">Theme</span>
              <ThemeToggle />
            </div>
            <div className="mt-4">an evaluator v2 · v2 web</div>
          </>
        )}
      </div>
    </aside>
  );
}

// -- icons: inline, 1.6px strokes, no dependency ------------------------------

function ChevronIcon({ flipped }: { flipped: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6"
         strokeLinecap="round" strokeLinejoin="round" className={flipped ? "rotate-180" : ""} aria-hidden>
      <path d="M10 3 5 8l5 5" />
    </svg>
  );
}

function ReportIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <rect x="3" y="3" width="14" height="14" rx="2" />
      <path d="M3 8h14M8 8v9" />
    </svg>
  );
}

function ChatIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h7A2.5 2.5 0 0 1 16 5.5v5a2.5 2.5 0 0 1-2.5 2.5H9l-4 3v-3H6.5A2.5 2.5 0 0 1 4 10.5v-5Z" />
    </svg>
  );
}

function RecordsIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d="M6 3h6l4 4v10a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" />
      <path d="M12 3v4h4M8 11h5M8 14h5" />
    </svg>
  );
}
