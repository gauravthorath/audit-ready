/**
 * Small presentational pieces shared by the report and chat pages.
 * Presentation only — none of these compute a verdict or a score.
 */

import type { CheckResult, PairStatus, SecurityFlag, Verdict } from "@/lib/api";

const VERDICT_STYLE: Record<string, { label: string; cls: string; dot: string }> = {
  satisfied: { label: "satisfied", cls: "bg-green-bg text-green border-green/30", dot: "bg-green" },
  partial: { label: "partial", cls: "bg-amber-bg text-amber border-amber/30", dot: "bg-amber" },
  gap: { label: "gap", cls: "bg-red-bg text-red border-red/30", dot: "bg-red" },
  not_applicable: { label: "not applicable", cls: "bg-neutral text-dim border-line", dot: "bg-dim" },
};

export function VerdictBadge({ verdict }: { verdict: Verdict | string | null | undefined }) {
  const s = (verdict && VERDICT_STYLE[verdict]) || {
    label: verdict ?? "no verdict",
    cls: "bg-neutral text-dim border-line",
    dot: "bg-dim",
  };
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[12.5px] font-semibold ${s.cls}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${s.dot}`} aria-hidden />
      {s.label}
    </span>
  );
}

export function StatusBadge({ status }: { status: PairStatus }) {
  const map: Record<PairStatus, { label: string; cls: string }> = {
    verdict: { label: "verified", cls: "text-green" },
    needs_review: { label: "needs human review", cls: "text-amber" },
    error: { label: "error", cls: "text-red" },
  };
  const s = map[status];
  return <span className={`text-[12.5px] font-semibold ${s.cls}`}>{s.label}</span>;
}

export function StatTile({
  label,
  value,
  sub,
  tone = "ink",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "ink" | "green" | "amber" | "red";
}) {
  const color = { ink: "text-ink", green: "text-green", amber: "text-amber", red: "text-red" }[tone];
  return (
    <div className="card px-5 py-4">
      <div className="eyebrow">{label}</div>
      <div className={`mono mt-2 text-[26px] font-semibold leading-none tracking-tight whitespace-nowrap ${color}`}>
        {value}
      </div>
      {sub && <div className="mt-2 text-[12.5px] text-dim">{sub}</div>}
    </div>
  );
}

/**
 * The score ledger: readiness with its denominator beside it — the one thing
 * this product insists on — then the counts as a ruled row. Wraps, never clips.
 */
export function ScoreLedger({
  readiness,
  verified,
  total,
  gaps,
  review,
  cost,
  costBasis,
}: {
  readiness: number | null;
  verified: number;
  total: number;
  gaps: number;
  review: number;
  cost: string | null;
  costBasis?: string;
}) {
  const pct = readiness === null ? null : Math.round(readiness * 100);
  const tone =
    pct === null ? "text-dim" : pct >= 80 ? "text-green" : pct >= 50 ? "text-amber" : "text-red";
  return (
    <div className="ledger" role="group" aria-label="Audit score">
      <div className="ledger-score">
        <div>
          <div className="eyebrow">Readiness</div>
          <div className={`pct mt-1.5 ${tone}`}>{pct === null ? "—" : `${pct}%`}</div>
          <div className="mt-1.5 text-[12px] text-dim">
            {pct === null ? "nothing applicable to score" : "weighted by severity"}
          </div>
        </div>
        <div>
          <div className="eyebrow">Verified</div>
          <div className="ledger-frac mt-2">
            <span className="num">{verified}</span>
            <span className="den">{total}</span>
          </div>
          <div className="mt-1.5 text-[12px] text-dim">pairs — the denominator</div>
        </div>
      </div>
      <div className="ledger-counts">
        <div>
          <div className="eyebrow">Open gaps</div>
          <div className={`val mt-2 ${gaps ? "text-red" : "text-ink"}`}>{gaps}</div>
          <div className="mt-1.5 text-[12px] text-dim">work to do</div>
        </div>
        <div>
          <div className="eyebrow">Needs review</div>
          <div className={`val mt-2 ${review ? "text-amber" : "text-ink"}`}>{review}</div>
          <div className="mt-1.5 text-[12px] text-dim">parked for a human</div>
        </div>
        <div>
          <div className="eyebrow">Est. cost</div>
          <div className="val mt-2 text-ink">{cost ?? "—"}</div>
          <div className="mt-1.5 text-[12px] text-dim">{costBasis ?? "model calls"}</div>
        </div>
      </div>
    </div>
  );
}

export function RateBar({ rate }: { rate: number | null }) {
  if (rate === null) return <span className="text-dim">—</span>;
  const pct = Math.round(rate * 100);
  const tone = pct >= 80 ? "bg-green" : pct >= 50 ? "bg-amber" : "bg-red";
  return (
    <div className="flex items-center gap-2 min-w-[140px]">
      <div className="h-1.5 flex-1 rounded-full bg-neutral overflow-hidden">
        <div className={`h-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="mono text-[12px] w-9 text-right">{pct}%</span>
    </div>
  );
}

export function Banner({
  tone,
  children,
}: {
  tone: "red" | "amber" | "blue" | "green";
  children: React.ReactNode;
}) {
  const cls = {
    red: "bg-red-bg text-red border-red/30",
    amber: "bg-amber-bg text-amber border-amber/30",
    blue: "bg-blue-bg text-blue border-blue/30",
    green: "bg-green-bg text-green border-green/30",
  }[tone];
  return <div className={`rounded-md border px-3.5 py-2.5 text-[13.5px] ${cls}`}>{children}</div>;
}

export function Code({ children }: { children: React.ReactNode }) {
  return (
    <code className="mono rounded bg-neutral px-1.5 py-0.5 text-[12.5px] text-body">{children}</code>
  );
}

/** One check_ticket verdict: badge, rationale, citations, quarantine, RAG trail. */
export function CheckResultView({ r }: { r: CheckResult }) {
  const check = r.citation_check ?? {};
  const flags = r.security_flags ?? [];
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="font-semibold">{r.control_id}</span>
        <VerdictBadge verdict={r.verdict} />
        {r.severity && <span className="text-[12.5px] text-dim">severity: {r.severity}</span>}
      </div>
      <p className="italic text-body leading-relaxed">{r.rationale}</p>
      {(r.citations?.length ?? 0) > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-[13px]">
          <span className="font-semibold">Citations:</span>
          {(r.citations ?? []).map((c, i) => (
            <Code key={i}>{c}</Code>
          ))}
        </div>
      )}
      {check.status && check.status !== "ok" && (
        <Banner tone="amber">
          Citation check: {check.status} — {check.note ?? ""}
        </Banner>
      )}
      {flags.length > 0 && (
        <Banner tone="red">
          🛡 Prompt-injection attempt quarantined at:{" "}
          <span className="mono">{flags.map((f) => String((f as SecurityFlag).path ?? "")).join(", ")}</span> — needs human review
        </Banner>
      )}
      {(r.evidence_queries?.length || r.evidence_chunks?.length) && (
        <details className="rounded-md border border-line bg-neutral/60 px-3.5 py-2.5">
          <summary className="cursor-pointer text-[13px] font-semibold text-body">
            RAG trail — how evidence was searched
          </summary>
          <div className="mt-3 flex flex-col gap-2.5 text-[13px]">
            <div>
              <div className="font-semibold">Evidence sub-queries</div>
              <ul className="mt-1 list-disc pl-5 text-body">
                {(r.evidence_queries ?? []).map((q, i) => (
                  <li key={i}>{q}</li>
                ))}
              </ul>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-semibold">Evidence chunks retrieved:</span>
              {(r.evidence_chunks ?? []).map((c, i) => (
                <Code key={i}>{c}</Code>
              ))}
            </div>
          </div>
        </details>
      )}
    </div>
  );
}
