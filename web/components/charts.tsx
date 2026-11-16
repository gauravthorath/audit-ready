/**
 * Charts - pure CSS/SVG, no library. Presentation only: every number drawn
 * here was counted by the engine; nothing is derived except pixel widths.
 *
 * Rules these follow (dataviz method): verdicts are *status* colours, so they
 * always ship with a label - a legend under every stack, a tooltip on every
 * mark, a table twin one toggle away. Marks are thin (≤14px stacks, ≤24px
 * columns); a 2px surface gap separates touching fills; chrome is recessive.
 */

// ---------------------------------------------------------------------------
// Verdict distribution - a stacked bar with a labelled legend
// ---------------------------------------------------------------------------

export type VerdictKey = "satisfied" | "partial" | "gap" | "not_applicable" | "needs_review" | "error";

/** Fixed order: best → worst, then the parked states. Never re-sorted by size. */
export const VERDICT_ORDER: { key: VerdictKey; label: string }[] = [
  { key: "satisfied", label: "satisfied" },
  { key: "partial", label: "partial" },
  { key: "gap", label: "gap" },
  { key: "not_applicable", label: "n/a" },
  { key: "needs_review", label: "needs review" },
  { key: "error", label: "run error" },
];

export type Counts = Partial<Record<VerdictKey, number>> & { [k: string]: number | undefined };

export function totalOf(counts: Counts): number {
  return VERDICT_ORDER.reduce((s, v) => s + (counts[v.key] ?? 0), 0);
}

/**
 * The stack. `legend` adds the swatch row with counts (always on when the bar
 * stands alone; off inside a table row where the columns are the legend).
 */
export function VerdictStack({
  counts,
  size = "sm",
  legend = false,
  className = "",
}: {
  counts: Counts;
  size?: "sm" | "lg";
  legend?: boolean;
  className?: string;
}) {
  const total = totalOf(counts);
  return (
    <div className={className}>
      <div className={`stack ${size === "lg" ? "lg" : ""}`} role="img" aria-label={describe(counts)}>
        {total === 0 && <div className="seg" style={{ width: "100%", background: "transparent" }} />}
        {VERDICT_ORDER.map(({ key, label }) => {
          const n = counts[key] ?? 0;
          if (!n) return null;
          return (
            <div
              key={key}
              className={`seg seg-${key} tip`}
              style={{ width: `${(100 * n) / total}%` }}
              data-tip={`${label} · ${n} of ${total}`}
              tabIndex={0}
              aria-label={`${label}: ${n} of ${total}`}
            />
          );
        })}
      </div>
      {legend && <VerdictLegend counts={counts} className="mt-2.5" />}
    </div>
  );
}

export function VerdictLegend({ counts, className = "" }: { counts: Counts; className?: string }) {
  return (
    <div className={`legend ${className}`}>
      {VERDICT_ORDER.map(({ key, label }) => {
        const n = counts[key] ?? 0;
        return (
          <span key={key} className={n ? "" : "opacity-50"}>
            <span className={`sw dot-${key}`} aria-hidden />
            {label} <span className="mono text-ink">{n}</span>
          </span>
        );
      })}
    </div>
  );
}

function describe(counts: Counts): string {
  return VERDICT_ORDER.filter((v) => counts[v.key]).map((v) => `${v.label} ${counts[v.key]}`).join(", ") || "no pairs";
}

/** Sum a set of per-control count maps into one. */
export function sumCounts(all: Counts[]): Counts {
  const out: Counts = {};
  for (const c of all) for (const v of VERDICT_ORDER) out[v.key] = (out[v.key] ?? 0) + (c[v.key] ?? 0);
  return out;
}

// ---------------------------------------------------------------------------
// Per-control rows - label · stack · rate. The compact "chart" form of the
// per-control table; the table is its twin.
// ---------------------------------------------------------------------------

export function ControlStacks({
  rows,
}: {
  rows: { id: string; title: string; counts: Counts; rate: number | null }[];
}) {
  return (
    <div className="flex flex-col gap-2">
      {rows.map((r) => (
        <div key={r.id} className="hbar" style={{ gridTemplateColumns: "minmax(120px, 220px) 1fr 44px" }}>
          <span className="truncate text-body" title={r.title}>
            {r.title}
          </span>
          <VerdictStack counts={r.counts} />
          <span className="mono text-right text-[12px] text-dim">
            {r.rate === null ? "-" : `${Math.round(r.rate * 100)}%`}
          </span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Facet bars - one hue, magnitude only. Each bar is a filter toggle.
// ---------------------------------------------------------------------------

export function FacetBars({
  title,
  items,
  selected,
  onToggle,
}: {
  title: string;
  items: { key: string; label: string; n: number }[];
  selected: Set<string>;
  onToggle: (key: string) => void;
}) {
  const max = Math.max(1, ...items.map((i) => i.n));
  const anySelected = selected.size > 0;
  return (
    <div>
      <div className="eyebrow mb-2">{title}</div>
      <div className="flex flex-col gap-0.5">
        {items.map((it) => {
          const on = !anySelected || selected.has(it.key);
          return (
            <button
              key={it.key}
              type="button"
              className="hbar"
              aria-pressed={on}
              onClick={() => onToggle(it.key)}
              title={`${on && anySelected ? "Remove" : "Filter by"} ${it.label}`}
            >
              <span className="truncate">{it.label || "-"}</span>
              <span className="track">
                <span className="fill block" style={{ width: `${(100 * it.n) / max}%` }} />
              </span>
              <span className="mono text-[12px] text-ink">{it.n}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Columns - a short series over time (runs, turns). Stacked when `values` has
// more than one entry; the legend names the layers.
// ---------------------------------------------------------------------------

export function Columns({
  points,
  series,
  current,
  format = (v) => String(v),
  ariaLabel,
}: {
  points: { key: string; label: string; values: number[] }[];
  /** One entry per stacked layer; omit for a single series (no legend). */
  series?: string[];
  /** Key of the point to highlight (e.g. the open run). */
  current?: string | null;
  format?: (v: number) => string;
  ariaLabel: string;
}) {
  const totals = points.map((p) => p.values.reduce((a, b) => a + b, 0));
  const max = Math.max(1, ...totals);
  return (
    <div>
      <div className="cols" role="img" aria-label={ariaLabel}>
        {points.map((p, i) => {
          const tip =
            `${p.label} · ` +
            (series && p.values.length > 1
              ? p.values.map((v, k) => `${series[k]} ${format(v)}`).join(", ")
              : format(totals[i]));
          return (
            <div
              key={p.key}
              className={`col tip ${p.key === current ? "current" : ""}`}
              data-tip={tip}
              tabIndex={0}
              aria-label={tip}
            >
              {p.values.map((v, k) => (
                <div
                  key={k}
                  className="col-fill"
                  style={{
                    height: `${(100 * v) / max}%`,
                    opacity: k === 0 ? 1 : 0.45,
                  }}
                />
              ))}
            </div>
          );
        })}
      </div>
      {series && series.length > 1 && (
        <div className="legend mt-2">
          {series.map((s, k) => (
            <span key={s}>
              <span className="sw" style={{ background: "var(--color-blue)", opacity: k === 0 ? 1 : 0.45 }} aria-hidden />
              {s}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
