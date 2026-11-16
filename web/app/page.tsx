"use client";

/**
 * The audit report - the product surface (CR-2, CR-8).
 *
 * Reads exactly what audit/report_view.py reads, over HTTP: packs, records,
 * a run started with POST /audit, progress over SSE, the finished report from
 * GET /audit/{id}. Nothing on this page computes a score; it lays out the one
 * the engine returned, denominator beside it.
 */

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  streamRun,
  type Pack,
  type PairRow,
  type Progress,
  type RecordSummary,
  type Report,
  type RunSnapshot,
} from "@/lib/api";
import {
  Banner,
  CheckResultView,
  Code,
  RateBar,
  ScoreLedger,
  StatusBadge,
  VerdictBadge,
} from "@/components/ui";
import { Button } from "@/components/ui/button";
import { Columns, ControlStacks, sumCounts, VerdictLegend, VerdictStack, type Counts } from "@/components/charts";
import { useViewMode, ViewToggle } from "@/components/view-toggle";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

type Tab = "gaps" | "review" | "all" | "errors";

export default function ReportPage() {
  const [packs, setPacks] = useState<Pack[]>([]);
  const [records, setRecords] = useState<RecordSummary[]>([]);
  const [packId, setPackId] = useState<string>("");
  const [ticketSel, setTicketSel] = useState<Set<string>>(new Set());
  const [controlSel, setControlSel] = useState<Set<string>>(new Set());
  const [loadError, setLoadError] = useState<string | null>(null);

  const [runId, setRunId] = useState<string | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [history, setHistory] = useState<RunSnapshot[]>([]);
  // readiness per finished run, for the trend. The list endpoint strips the
  // report on purpose (it is for picking), so each done run is fetched once.
  const [scores, setScores] = useState<Record<string, number | null>>({});
  const [tab, setTab] = useState<Tab>("gaps");
  const unsubscribe = useRef<(() => void) | null>(null);

  const pack = useMemo(() => packs.find((p) => p.id === packId) ?? null, [packs, packId]);

  // -- load the pickers --------------------------------------------------
  // The API takes ~20 s to load its embedding model on a cold start. A page
  // opened before that must not sit with an empty picker until someone
  // reloads it, so a failed load retries every 3 s until the API answers.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () =>
      Promise.all([api.packs(), api.records(), api.runs()])
        .then(([ps, rs, runs]) => {
          if (cancelled) return;
          setPacks(ps);
          setRecords(rs);
          setHistory(runs);
          setLoadError(null);
          const first = ps.find((p) => p.id && !p.error);
          if (first?.id) {
            setPackId(first.id);
            setControlSel(new Set(first.controls.map((c) => c.control_id)));
          }
          setTicketSel(new Set(rs.map((r) => r.ticket_id)));
        })
        .catch((e: unknown) => {
          if (cancelled) return;
          setLoadError(`${e instanceof Error ? e.message : String(e)} - retrying…`);
          timer = setTimeout(load, 3000);
        });
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  // switching packs resets the control scope to "all of them"
  const choosePack = (id: string) => {
    setPackId(id);
    const p = packs.find((x) => x.id === id);
    setControlSel(new Set(p?.controls.map((c) => c.control_id) ?? []));
  };

  const toggle = (set: Set<string>, id: string, setter: (s: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setter(next);
  };

  const nPairs = ticketSel.size * controlSel.size;

  // -- run ------------------------------------------------------------------
  // A terminal snapshot lands in three places: the report pane, the history
  // row (status flips from running → done), and the trend (its score).
  const adopt = useCallback((s: RunSnapshot) => {
    setSnapshot(s);
    if (s.status !== "running") {
      setHistory((prev) => prev.map((h) => (h.run_id === s.run_id ? { ...h, status: s.status, cost: s.cost } : h)));
      if (s.status === "done") setScores((prev) => ({ ...prev, [s.run_id]: s.report?.readiness_score ?? null }));
    }
  }, []);

  const openRun = useCallback((id: string) => {
    unsubscribe.current?.();
    setRunId(id);
    setSnapshot(null);
    setRunError(null);
    setProgress(null);
    api.run(id).then((s) => {
      setProgress(s.progress);
      if (s.status !== "running") {
        adopt(s);
        return;
      }
      unsubscribe.current = streamRun(id, {
        onProgress: (p) => setProgress(p.progress),
        onEnd: () => api.run(id).then(adopt).catch((e: Error) => setRunError(e.message)),
        onError: () => {
          // SSE dropped: fall back to polling until terminal
          const t = setInterval(() => {
            api.run(id).then((s2) => {
              setProgress(s2.progress);
              if (s2.status !== "running") {
                clearInterval(t);
                adopt(s2);
              }
            });
          }, 1000);
        },
      });
    });
  }, [adopt]);

  useEffect(() => () => unsubscribe.current?.(), []);

  useEffect(() => {
    const missing = history.filter((h) => h.status === "done" && !(h.run_id in scores));
    if (missing.length === 0) return;
    let cancelled = false;
    Promise.all(
      missing.map((h) =>
        api
          .run(h.run_id)
          .then((s) => [h.run_id, s.report?.readiness_score ?? null] as const)
          .catch(() => [h.run_id, null] as const),
      ),
    ).then((pairs) => {
      if (!cancelled) setScores((prev) => ({ ...prev, ...Object.fromEntries(pairs) }));
    });
    return () => {
      cancelled = true;
    };
  }, [history, scores]);

  // the trend compares like with like: runs of the pack that is selected
  const trend = useMemo(() => {
    const runs = history
      .filter((h) => h.status === "done" && (!packId || h.pack_id === packId) && typeof scores[h.run_id] === "number")
      .sort((a, b) => a.started_at.localeCompare(b.started_at));
    return runs.map((h) => ({
      key: h.run_id,
      label: `${h.run_id} · ${h.started_at.slice(0, 16).replace("T", " ")}`,
      values: [Math.round((scores[h.run_id] as number) * 100)],
    }));
  }, [history, scores, packId]);

  const start = async () => {
    if (!pack?.id) return;
    setStarting(true);
    setRunError(null);
    try {
      const { run_id } = await api.startAudit({
        pack_id: pack.id,
        ticket_ids: [...ticketSel],
        control_ids: [...controlSel],
      });
      setHistory(await api.runs());
      openRun(run_id);
      setTab("gaps");
    } catch (e) {
      setRunError(e instanceof ApiError ? e.message : "Could not start the audit.");
    } finally {
      setStarting(false);
    }
  };

  const report = snapshot?.status === "done" ? snapshot.report ?? null : null;

  return (
    <div className="mx-auto max-w-[1320px]">
      <header className="mb-6">
        <div className="eyebrow">Audit-readiness report</div>
        <h1 className="mt-1 text-[28px] font-semibold tracking-tight">
          Every record against every control.
        </h1>
        <p className="mt-1 text-[14px] text-body">
          Pick a pack and a scope, run it, and read the score with its denominator beside it.
          The system drafts. A person signs.
        </p>
      </header>

      {loadError && <Banner tone="red">{loadError}</Banner>}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[300px_minmax(0,1fr)]">
        {/* -- scope ----------------------------------------------------- */}
        <section className="card grid gap-5 self-start p-5 md:grid-cols-3 xl:grid-cols-1">
          <div>
            <div className="eyebrow mb-2">Checklist pack</div>
            <Select value={packId} onValueChange={choosePack}>
              <SelectTrigger className="h-10 w-full bg-paper" aria-label="Checklist pack">
                <SelectValue placeholder="Choose a pack" />
              </SelectTrigger>
              <SelectContent>
                {packs.map((p) => (
                  <SelectItem key={p.path} value={p.id ?? p.path} disabled={!p.id}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {pack?.description && (
              <p className="mt-2 text-[12.5px] leading-snug text-dim">{pack.description}</p>
            )}
          </div>

          <ScopeList
            title="Controls"
            items={(pack?.controls ?? []).map((c) => ({
              id: c.control_id,
              label: c.title,
              sub: `${c.clause} · ${c.severity}`,
            }))}
            selected={controlSel}
            onToggle={(id) => toggle(controlSel, id, setControlSel)}
            onAll={() => setControlSel(new Set(pack?.controls.map((c) => c.control_id) ?? []))}
            onNone={() => setControlSel(new Set())}
          />

          <ScopeList
            title="Records"
            items={records.map((r) => ({ id: r.ticket_id, label: r.ticket_id, sub: r.title ?? undefined }))}
            selected={ticketSel}
            onToggle={(id) => toggle(ticketSel, id, setTicketSel)}
            onAll={() => setTicketSel(new Set(records.map((r) => r.ticket_id)))}
            onNone={() => setTicketSel(new Set())}
            compact
          />

          <div className="border-t border-line pt-4 md:col-span-3 xl:col-span-1">
            <div className="text-[12.5px] text-dim">
              <span className="mono text-ink">{nPairs}</span> pairs → about{" "}
              <span className="mono">{2 * nPairs}-{3 * nPairs}</span> model calls (judge + verify,
              maybe one re-judge).
            </div>
            <Button
              size="lg"
              className="mt-3 w-full"
              onClick={start}
              disabled={starting || nPairs === 0 || !pack?.id}
            >
              {starting ? "Starting…" : "Run audit"}
            </Button>
            {runError && <div className="mt-3"><Banner tone="red">{runError}</Banner></div>}
          </div>

          {history.length > 0 && (
            <div className="border-t border-line pt-4 md:col-span-3 xl:col-span-1">
              <div className="eyebrow mb-2">Runs this session</div>
              {trend.length >= 2 && (
                <div className="mb-3">
                  <div className="mb-1.5 flex items-baseline justify-between text-[12px] text-dim">
                    <span>Readiness over runs · {pack?.id ?? "all packs"}</span>
                    <span className="mono">{trend[trend.length - 1].values[0]}%</span>
                  </div>
                  <Columns
                    points={trend}
                    current={runId}
                    format={(v) => `${v}%`}
                    ariaLabel={`Readiness score for ${trend.length} runs of ${pack?.id ?? "all packs"}`}
                  />
                </div>
              )}
              <ul className="flex flex-col gap-1">
                {history.map((h) => (
                  <li key={h.run_id}>
                    <button
                      className={`w-full rounded px-2 py-1.5 text-left text-[12.5px] hover:bg-neutral ${
                        h.run_id === runId ? "bg-neutral" : ""
                      }`}
                      onClick={() => openRun(h.run_id)}
                    >
                      <span className="mono">{h.run_id}</span> · {h.pack_id} ·{" "}
                      <span className={h.status === "error" ? "text-red" : "text-dim"}>{h.status}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>

        {/* -- report ---------------------------------------------------- */}
        <section className="min-w-0 flex flex-col gap-5">
          {!runId && (
            <div className="card px-6 py-10 text-center text-body">
              <div className="text-[15px]">
                Pick a pack and scope, then <span className="font-semibold">Run audit</span>.
              </div>
              <div className="mt-1 text-[13px] text-dim">
                The report appears here with the score, open gaps, the review queue, and the agent
                trace for every pair.
              </div>
            </div>
          )}

          {runId && !report && snapshot?.status !== "error" && (
            <div className="card p-5">
              <div className="flex items-center justify-between">
                <div className="font-semibold">
                  Auditing {progress?.total ?? "…"} record-control pairs…
                </div>
                <div className="mono text-[13px] text-dim">
                  {progress ? `${progress.done}/${progress.total}` : ""}
                </div>
              </div>
              <div className="mt-3 h-2 rounded-full bg-neutral overflow-hidden">
                <div
                  className="h-full bg-blue transition-[width] duration-300"
                  style={{ width: progress && progress.total ? `${(100 * progress.done) / progress.total}%` : "2%" }}
                />
              </div>
              <div className="mono mt-2 text-[12.5px] text-dim">{progress?.label ?? ""}</div>
            </div>
          )}

          {snapshot?.status === "error" && (
            <Banner tone="red">The run failed: {snapshot.error}</Banner>
          )}

          {report && snapshot && <ReportView report={report} snapshot={snapshot} tab={tab} setTab={setTab} />}
        </section>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function ScopeList({
  title,
  items,
  selected,
  onToggle,
  onAll,
  onNone,
  compact,
}: {
  title: string;
  items: { id: string; label: string; sub?: string }[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onAll: () => void;
  onNone: () => void;
  compact?: boolean;
}) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <div className="eyebrow">
          {title} <span className="normal-case tracking-normal text-body">· {selected.size}/{items.length}</span>
        </div>
        <div className="flex gap-2 text-[12px]">
          <button className="text-blue hover:underline" onClick={onAll}>all</button>
          <button className="text-blue hover:underline" onClick={onNone}>none</button>
        </div>
      </div>
      <ul className={`flex flex-col gap-1 overflow-auto pr-1 ${compact ? "max-h-[180px]" : "max-h-[260px]"}`}>
        {items.map((it) => (
          <li key={it.id}>
            <label className="flex cursor-pointer items-start gap-2.5 rounded px-1.5 py-1 hover:bg-neutral">
              <input
                type="checkbox"
                className="mt-1 accent-green"
                checked={selected.has(it.id)}
                onChange={() => onToggle(it.id)}
              />
              <span className="min-w-0">
                <span className={`block text-[13px] ${compact ? "mono" : "font-medium"}`}>{it.label}</span>
                {it.sub && <span className="block truncate text-[12px] text-dim">{it.sub}</span>}
              </span>
            </label>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ReportView({
  report,
  snapshot,
  tab,
  setTab,
}: {
  report: Report;
  snapshot: RunSnapshot;
  tab: Tab;
  setTab: (t: Tab) => void;
}) {
  const rowsFor: Record<Tab, PairRow[]> = {
    gaps: report.open_gaps,
    review: report.review_queue,
    all: report.rows,
    errors: report.errors,
  };
  const tabs: { key: Tab; label: string; n: number }[] = [
    { key: "gaps", label: "Open gaps", n: report.open_gaps.length },
    { key: "review", label: "Needs review", n: report.review_queue.length },
    { key: "all", label: "All pairs", n: report.rows.length },
    { key: "errors", label: "Errors", n: report.errors.length },
  ];
  const score = report.readiness_score;

  const [controlView, setControlView] = useViewMode("report.controls");
  const [pairView, setPairView] = useViewMode("report.pairs");
  const [openPair, setOpenPair] = useState<string | null>(null);

  const controls = report.control_ids.map((cid) => ({ id: cid, ...report.per_control[cid] }));
  const mix = useMemo(() => sumCounts(controls.map((c) => c.counts as Counts)), [controls]);
  const rows = rowsFor[tab];
  const pairKey = (r: PairRow) => `${r.ticket_id}-${r.control_id}`;

  return (
    <>
      <div className="text-[12.5px] text-dim">
        Pack <span className="font-semibold text-ink">{report.standard}</span> (<Code>{report.pack_id}</Code>)
        · generated {report.generated_at} · {report.ticket_ids.length} records × {report.control_ids.length} controls
        {report.elapsed_ms ? <> · took {(report.elapsed_ms / 1000).toFixed(1)}s</> : null}
      </div>

      <ScoreLedger
        readiness={score}
        verified={report.verified_pairs}
        total={report.total_pairs}
        gaps={report.open_gaps.length}
        review={report.review_queue.length}
        cost={snapshot.cost ? `$${snapshot.cost.usd.toFixed(4)}` : null}
        costBasis={snapshot.cost?.basis}
      />

      {/* -- verdict mix: every pair, one bar ------------------------------ */}
      <div className="card px-5 py-4">
        <div className="mb-3 flex items-baseline justify-between">
          <div className="font-semibold">Verdict mix</div>
          <div className="text-[12px] text-dim">{report.total_pairs} pairs, every verdict and parked state</div>
        </div>
        <VerdictStack counts={mix} size="lg" legend />
      </div>

      {/* -- per control ----------------------------------------------------- */}
      <div className="card overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-3.5">
          <div className="font-semibold">Per control</div>
          <div className="flex items-center gap-2">
            <ViewToggle value={controlView} onChange={setControlView} label="Per-control view" />
            <Button variant="outline" size="sm" asChild>
              <a href={api.reportMarkdownUrl(snapshot.run_id)} target="_blank" rel="noreferrer">
                Export Markdown
              </a>
            </Button>
          </div>
        </div>

        {controlView === "list" ? (
          <div className="max-h-[420px] overflow-auto border-t border-line">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Control</th><th>Clause</th><th>Severity</th>
                  <th className="num">Satisfied</th><th className="num">Partial</th><th className="num">Gap</th>
                  <th className="num hidden 2xl:table-cell">N/A</th><th className="num">Needs review</th>
                  <th className="num hidden 2xl:table-cell">Run error</th>
                  <th>Satisfied rate</th>
                </tr>
              </thead>
              <tbody>
                {controls.map((pc) => {
                  const c = pc.counts;
                  return (
                    <tr key={pc.id}>
                      <td className="min-w-[200px] font-medium">{pc.title}</td>
                      <td className="mono text-[12.5px]">{pc.clause}</td>
                      <td className="text-body">{pc.severity}</td>
                      <td className={`num ${c.satisfied ? "" : "zero"}`}>{c.satisfied}</td>
                      <td className={`num ${c.partial ? "" : "zero"}`}>{c.partial}</td>
                      <td className={`num ${c.gap ? "text-red font-semibold" : "zero"}`}>{c.gap}</td>
                      <td className={`num hidden 2xl:table-cell ${c.not_applicable ? "" : "zero"}`}>{c.not_applicable}</td>
                      <td className={`num ${c.needs_review ? "text-amber font-semibold" : "zero"}`}>{c.needs_review}</td>
                      <td className={`num hidden 2xl:table-cell ${c.error ? "text-red font-semibold" : "zero"}`}>{c.error}</td>
                      <td><RateBar rate={pc.satisfied_rate} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="border-t border-line p-4">
            <div className="mb-4 rounded-lg border border-line bg-neutral/40 px-4 py-3">
              <div className="eyebrow mb-2.5">All controls at a glance</div>
              <ControlStacks
                rows={controls.map((c) => ({ id: c.id, title: c.title, counts: c.counts as Counts, rate: c.satisfied_rate }))}
              />
            </div>
            <div className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-3">
              {controls.map((pc) => {
                const rate = pc.satisfied_rate === null ? null : Math.round(pc.satisfied_rate * 100);
                const tone = rate === null ? "text-dim" : rate >= 80 ? "text-green" : rate >= 50 ? "text-amber" : "text-red";
                return (
                  <div key={pc.id} className="tile flex flex-col gap-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-[14px] font-semibold" title={pc.title}>{pc.title}</div>
                        <div className="mt-0.5 text-[12px] text-dim">
                          <span className="mono">{pc.clause}</span> · {pc.severity}
                        </div>
                      </div>
                      <div className={`mono text-[20px] font-semibold leading-none ${tone}`}>
                        {rate === null ? "-" : `${rate}%`}
                      </div>
                    </div>
                    <VerdictStack counts={pc.counts as Counts} />
                    <VerdictLegend counts={pc.counts as Counts} />
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* -- pairs ----------------------------------------------------------- */}
      <div className="card">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line pr-3">
          <Tabs value={tab} onValueChange={(v) => { setTab(v as Tab); setOpenPair(null); }}>
            <TabsList variant="line" className="justify-start gap-1 px-3 pt-1.5">
              {tabs.map((t) => (
                <TabsTrigger key={t.key} value={t.key} className="flex-none px-3 text-[13.5px] data-active:font-semibold">
                  {t.label} <span className="mono text-[12px] text-dim">({t.n})</span>
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
          <ViewToggle value={pairView} onChange={setPairView} label="Pairs view" />
        </div>

        {rows.length === 0 && (
          <div className="px-5 py-6 text-center text-[13.5px] text-dim">Nothing here.</div>
        )}

        {rows.length > 0 && pairView === "list" && (
          <ul className="divide-y divide-line">
            {rows.map((r) => <PairItem key={pairKey(r)} r={r} />)}
          </ul>
        )}

        {rows.length > 0 && pairView === "grid" && (
          <div className="p-4">
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {rows.map((r) => {
                const k = pairKey(r);
                const selected = k === openPair;
                return (
                  <Fragment key={k}>
                    <button
                      type="button"
                      className={`tile flex flex-col gap-2 ${selected ? "selected" : ""}`}
                      aria-expanded={selected}
                      onClick={() => setOpenPair(selected ? null : k)}
                    >
                      <div className="flex min-w-0 items-center gap-2 text-[13px]">
                        <span className="mono shrink-0 whitespace-nowrap">{r.ticket_id}</span>
                        <span className="text-dim">×</span>
                        <span className="mono truncate" title={r.control_id}>{r.control_id}</span>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <VerdictBadge verdict={r.verdict} />
                        <StatusBadge status={r.status} />
                      </div>
                      {r.rationale && (
                        <p className="line-clamp-3 text-[12.5px] leading-snug text-body">{r.rationale}</p>
                      )}
                      <div className="mt-auto flex items-center justify-between text-[11.5px] text-dim">
                        <span>{r.citations?.length ?? 0} citation{(r.citations?.length ?? 0) === 1 ? "" : "s"}</span>
                        {r.attempts !== undefined && <span>{r.attempts} judge pass{r.attempts === 1 ? "" : "es"}</span>}
                      </div>
                    </button>
                    {/* the detail opens as a full-width row right under the tile's row */}
                    {selected && (
                      <div className="col-span-full rounded-lg border border-green/40 bg-paper">
                        <div className="flex items-center justify-between border-b border-line px-5 py-3">
                          <div className="flex items-center gap-2 text-[13px]">
                            <span className="mono">{r.ticket_id}</span>
                            <span className="text-dim">×</span>
                            <span className="mono">{r.control_id}</span>
                          </div>
                          <Button variant="ghost" size="sm" onClick={() => setOpenPair(null)}>Close</Button>
                        </div>
                        <PairDetail r={r} />
                      </div>
                    )}
                  </Fragment>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </>
  );
}

function PairItem({ r }: { r: PairRow }) {
  return (
    <li>
      <details className="group">
        <summary className="flex cursor-pointer flex-wrap items-center gap-3 px-5 py-3 hover:bg-neutral/60">
          <span className="mono text-[13px]">{r.ticket_id}</span>
          <span className="text-dim">×</span>
          <span className="mono text-[13px]">{r.control_id}</span>
          <span className="text-dim">-</span>
          <VerdictBadge verdict={r.verdict} />
          <span className="text-dim">·</span>
          <StatusBadge status={r.status} />
          {r.attempts !== undefined && (
            <span className="ml-auto text-[12px] text-dim">{r.attempts} judge pass{r.attempts === 1 ? "" : "es"}</span>
          )}
        </summary>
        <div className="border-t border-line">
          <PairDetail r={r} />
        </div>
      </details>
    </li>
  );
}

/** The body of one pair: verdict, verifier's view, agent trace. Shared by both views. */
function PairDetail({ r }: { r: PairRow }) {
  const ver = r.verification ?? null;
  return (
    <div className="flex flex-col gap-4 bg-paper px-5 py-4">
      {r.status === "error" ? (
        <Banner tone="red">{r.error ?? "This pair errored."}</Banner>
      ) : (
        <CheckResultView r={r} />
      )}
      {ver && (
        <div className="text-[13px] text-dim">
          Verifier {ver.agrees === null ? "could not answer" : ver.agrees ? "agrees" : "objects"}
          {ver.model && <> (<span className="mono">{ver.model}</span>)</>}
          {(ver.issues?.length ?? 0) > 0 && (
            <ul className="mt-1 list-disc pl-5 text-body">
              {(ver.issues ?? []).map((i, k) => <li key={k}>{i}</li>)}
            </ul>
          )}
        </div>
      )}
      {r.trace && r.trace.length > 0 && (
        <div>
          <div className="mb-1.5 text-[13px] font-semibold">Agent trace</div>
          <ol className="trace list-decimal flex flex-col gap-1 pl-6">
            {r.trace.map((line, i) => (
              <li key={i}><span className="rounded bg-green-bg px-1.5 py-0.5 text-green">{line}</span></li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
