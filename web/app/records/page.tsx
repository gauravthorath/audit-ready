"use client";

/** The evidence under audit, as the engine sees it. Read-only. */

import { useEffect, useMemo, useState } from "react";
import { Search, X } from "lucide-react";
import { api, type RecordSummary, type Ticket } from "@/lib/api";
import { Banner, Code } from "@/components/ui";
import { FacetBars } from "@/components/charts";
import { useViewMode, ViewToggle } from "@/components/view-toggle";

type Facet = "type" | "priority" | "status";
const FACETS: { key: Facet; title: string }[] = [
  { key: "type", title: "By type" },
  { key: "priority", title: "By priority" },
  { key: "status", title: "By status" },
];

export default function RecordsPage() {
  const [rows, setRows] = useState<RecordSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Ticket | null>(null);
  const [view, setView] = useViewMode("records");
  const [q, setQ] = useState("");
  const [filters, setFilters] = useState<Record<Facet, Set<string>>>({
    type: new Set(),
    priority: new Set(),
    status: new Set(),
  });

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () =>
      api.records()
        .then((rs) => { if (!cancelled) { setRows(rs); setError(null); } })
        .catch((e: Error) => {
          if (cancelled) return;
          setError(`${e.message} — retrying…`);
          timer = setTimeout(load, 3000);  // the API may still be loading its model
        });
    load();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, []);

  const show = (id: string) => api.record(id).then(setOpen).catch((e: Error) => setError(e.message));

  // Facet counts are taken over the *whole* corpus, not the filtered slice, so
  // a bar never disappears because you clicked it — colour follows the entity.
  const facets = useMemo(() => {
    const count = (key: Facet) => {
      const m = new Map<string, number>();
      for (const r of rows) {
        const v = String(r[key] ?? "");
        m.set(v, (m.get(v) ?? 0) + 1);
      }
      return [...m.entries()].sort((a, b) => b[1] - a[1]).map(([k, n]) => ({ key: k, label: k, n }));
    };
    return { type: count("type"), priority: count("priority"), status: count("status") };
  }, [rows]);

  const toggleFacet = (key: Facet, value: string) =>
    setFilters((f) => {
      const next = new Set(f[key]);
      if (next.has(value)) next.delete(value);
      else next.add(value);
      return { ...f, [key]: next };
    });
  const clear = () => {
    setFilters({ type: new Set(), priority: new Set(), status: new Set() });
    setQ("");
  };
  const active = q.trim() !== "" || FACETS.some((f) => filters[f.key].size > 0);

  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return rows.filter((r) => {
      for (const f of FACETS) {
        const sel = filters[f.key];
        if (sel.size > 0 && !sel.has(String(r[f.key] ?? ""))) return false;
      }
      if (!needle) return true;
      const hay = `${r.ticket_id} ${r.title ?? ""} ${(r.linked_changes ?? []).join(" ")} ${(r.linked_emails ?? []).join(" ")}`.toLowerCase();
      return hay.includes(needle);
    });
  }, [rows, filters, q]);

  return (
    <div className="mx-auto max-w-[1320px]">
      <header className="mb-6">
        <div className="eyebrow">Records</div>
        <h1 className="mt-1 text-[28px] font-semibold tracking-tight">The evidence under audit.</h1>
        <p className="mt-1 text-[14px] text-body">
          Tickets from the current corpus. Linked change and email records are joined by id when a
          control is judged.
        </p>
      </header>
      {error && <Banner tone="red">{error}</Banner>}

      {/* -- the corpus at a glance; every bar is a filter ------------------ */}
      {rows.length > 0 && (
        <section className="card mb-6 grid gap-5 px-5 py-4 md:grid-cols-3">
          {FACETS.map((f) => (
            <FacetBars
              key={f.key}
              title={f.title}
              items={facets[f.key]}
              selected={filters[f.key]}
              onToggle={(v) => toggleFacet(f.key, v)}
            />
          ))}
        </section>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_420px]">
        <div className="min-w-0">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <label className="relative flex-1 min-w-[220px] max-w-[420px]">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-dim" aria-hidden />
              <input
                type="search"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search id, title, linked ids…"
                aria-label="Search records"
                className="w-full rounded-md border border-line bg-paper py-2 pl-9 pr-3 text-[13.5px] outline-none focus:border-green"
              />
            </label>
            <div className="flex items-center gap-2">
              <span className="text-[12.5px] text-dim">
                <span className="mono text-ink">{visible.length}</span> of {rows.length}
              </span>
              {active && (
                <button type="button" className="chip !py-1 gap-1" onClick={clear}>
                  <X className="h-3 w-3" aria-hidden /> clear
                </button>
              )}
              <ViewToggle value={view} onChange={setView} label="Records view" />
            </div>
          </div>

          {view === "list" ? (
            <div className="card overflow-x-auto">
              <table className="tbl">
                <thead>
                  <tr><th>Ticket</th><th>Title</th><th>Type</th><th>Priority</th><th>Status</th><th className="num">Comments</th><th>Links</th></tr>
                </thead>
                <tbody>
                  {visible.map((r) => (
                    <tr key={r.ticket_id} className={`cursor-pointer hover:bg-neutral/60 ${open?.ticket_id === r.ticket_id ? "bg-green-bg/40" : ""}`} onClick={() => show(r.ticket_id)}>
                      <td className="mono">{r.ticket_id}</td>
                      <td className="font-medium">{r.title}</td>
                      <td>{r.type}</td>
                      <td>{r.priority}</td>
                      <td>{r.status}</td>
                      <td className="num">{r.comments}</td>
                      <td className="mono text-[12px] text-dim">{[...(r.linked_changes ?? []), ...(r.linked_emails ?? [])].join(", ") || "—"}</td>
                    </tr>
                  ))}
                  {visible.length === 0 && (
                    <tr><td colSpan={7} className="py-8 text-center text-dim">No records match.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-3">
              {visible.map((r) => {
                const links = [...(r.linked_changes ?? []), ...(r.linked_emails ?? [])];
                const selected = open?.ticket_id === r.ticket_id;
                return (
                  <button
                    key={r.ticket_id}
                    type="button"
                    className={`tile flex flex-col gap-2 ${selected ? "selected" : ""}`}
                    onClick={() => show(r.ticket_id)}
                    aria-pressed={selected}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="mono text-[12.5px] text-dim">{r.ticket_id}</span>
                      <span className="text-[11.5px] text-dim">{r.comments} comment{r.comments === 1 ? "" : "s"}</span>
                    </div>
                    <div className="line-clamp-2 text-[14px] font-semibold leading-snug">{r.title}</div>
                    <div className="flex flex-wrap gap-1.5 text-[11.5px]">
                      <span className="rounded-full border border-line bg-neutral px-2 py-0.5 text-body">{r.type}</span>
                      <span className="rounded-full border border-line bg-neutral px-2 py-0.5 text-body">{r.priority}</span>
                      <span className="rounded-full border border-line bg-neutral px-2 py-0.5 text-body">{r.status}</span>
                    </div>
                    <div className="mono mt-auto truncate text-[11.5px] text-dim" title={links.join(", ")}>
                      {links.length ? links.join(", ") : "no linked records"}
                    </div>
                  </button>
                );
              })}
              {visible.length === 0 && (
                <div className="card col-span-full py-8 text-center text-dim">No records match.</div>
              )}
            </div>
          )}
        </div>

        <aside className="card self-start p-5 text-[13.5px] xl:sticky xl:top-6">
          {!open ? (
            <div className="text-dim">Select a ticket to read it as the judge does.</div>
          ) : (
            <div className="flex flex-col gap-4">
              <div>
                <div className="mono text-[12.5px] text-dim">{open.ticket_id}</div>
                <div className="text-[16px] font-semibold">{open.title}</div>
                <div className="mt-1 text-[12.5px] text-dim">
                  {open.type} · {open.priority} · {open.status} · created {open.created}
                  {open.closed && <> · closed {open.closed}</>}
                </div>
              </div>
              <Field label="Description"><p className="leading-relaxed text-body">{open.description}</p></Field>
              <Field label={`Comments (${open.comments.length})`}>
                <ul className="flex flex-col gap-2">
                  {open.comments.map((c, i) => (
                    <li key={i} className="rounded-md bg-neutral px-3 py-2">
                      <div className="text-[12px] text-dim"><span className="font-semibold text-body">{c.author}</span> · <span className="mono">{c.timestamp}</span></div>
                      <div className="mt-1 leading-relaxed">{c.body}</div>
                    </li>
                  ))}
                </ul>
              </Field>
              <Field label="Resolution"><p className="text-body">{open.resolution || <span className="text-dim">— none recorded</span>}</p></Field>
              <Field label="Sign-off">
                {open.signoff ? <pre className="wrap mono text-[12px]">{JSON.stringify(open.signoff, null, 2)}</pre> : <span className="text-dim">— none recorded</span>}
              </Field>
              <Field label="Linked records">
                <div className="flex flex-wrap gap-1.5">
                  {[...open.linked_changes, ...open.linked_emails].map((id) => <Code key={id}>{id}</Code>)}
                  {open.linked_changes.length + open.linked_emails.length === 0 && <span className="text-dim">—</span>}
                </div>
              </Field>
              {typeof open.source_url === "string" && (
                <a className="text-blue hover:underline text-[13px]" href={open.source_url} target="_blank" rel="noreferrer">Open in source system ↗</a>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="eyebrow mb-1.5">{label}</div>
      {children}
    </div>
  );
}
