"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useSyncExternalStore } from "react";

const OPTIONS = [
  { value: "light", label: "Light", Icon: Sun },
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "system", label: "System", Icon: Monitor },
] as const;

// The stored theme is unknown on the server, so the toggle renders neutral
// until mounted. useSyncExternalStore gives "mounted" without a setState in
// an effect: server snapshot false, client snapshot true.
const useMounted = () =>
  useSyncExternalStore(() => () => {}, () => true, () => false);

/** Three-way segmented control for the expanded sidebar. */
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const mounted = useMounted();
  const current = mounted ? theme ?? "system" : null;
  return (
    <div className="seg-ctl" role="group" aria-label="Colour theme">
      {OPTIONS.map(({ value, label, Icon }) => (
        <button
          key={value}
          type="button"
          aria-pressed={current === value}
          onClick={() => setTheme(value)}
          title={label}
        >
          <Icon className="h-3.5 w-3.5" aria-hidden />
          <span className="sr-only">{label}</span>
        </button>
      ))}
    </div>
  );
}

/** One icon that cycles light → dark → system, for the collapsed sidebar. */
export function ThemeCycleButton() {
  const { theme, setTheme } = useTheme();
  const mounted = useMounted();
  const current = (mounted ? theme : undefined) ?? "system";
  const idx = OPTIONS.findIndex((o) => o.value === current);
  const next = OPTIONS[(idx + 1) % OPTIONS.length];
  const Icon = OPTIONS[Math.max(idx, 0)].Icon;
  return (
    <button
      type="button"
      onClick={() => setTheme(next.value)}
      className="rounded-md p-1.5 text-dim hover:bg-neutral hover:text-ink"
      title={`Theme: ${current}. Switch to ${next.label.toLowerCase()}`}
      aria-label={`Theme: ${current}. Switch to ${next.label.toLowerCase()}`}
    >
      <Icon className="h-4 w-4" aria-hidden />
    </button>
  );
}
