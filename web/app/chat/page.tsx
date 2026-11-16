"use client";

/**
 * Compliance chat - the v1 chatbot as one panel (CR-8).
 *
 * Same contract as assistant/app.py: the page renders what AuditAgent returns
 * and never re-runs anything. The sources panel is fed by the tool calls the
 * agent already made, which is what keeps the citations honest - nothing here
 * can cite a thing the agent did not retrieve.
 */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  api, ApiError, type ChatResponse, type CheckResult, type SessionSummary, type ToolCall,
} from "@/lib/api";
import { Banner, CheckResultView, Code } from "@/components/ui";
import { Button } from "@/components/ui/button";
import { Columns, ControlStacks, type Counts } from "@/components/charts";

// The exact prompts from docs/DEMO.md, so the live demo is one click each.
const EXAMPLES = [
  "Does ticket TCK-1001 satisfy the release sign-off control, ISO9001-8.6?",
  "Check TCK-1009 against ISO9001-7.5-documented-info. Is the documented information under control?",
  "Does TCK-1010 satisfy ISO9001-8.2.1-client-comm?",
  "Which of TCK-1001…TCK-1005 fail release sign-off?",
];

type Turn = {
  question: string;
  answer?: string;
  tool_calls?: ToolCall[];
  prompt_tokens?: number;
  completion_tokens?: number;
  error?: string;
  pending?: boolean;
};

// The conversation id survives a reload, and only a reload: sessionStorage is
// per tab and is cleared when the tab closes. The messages themselves live on
// the server (api/sessions.py), so what is kept here is a pointer, never
// evidence - if the tab forgets it, the conversation is still listed under
// "Earlier conversations".
const SESSION_KEY = "auditready.session";

const SESSION_EVENT = "auditready:session";

function rememberSession(id: string | undefined) {
  try {
    if (id) sessionStorage.setItem(SESSION_KEY, id);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* private window: the conversation still works, it just will not resume */
  }
  window.dispatchEvent(new Event(SESSION_EVENT));
}

/**
 * The stored id, read through `useSyncExternalStore` - the same pattern
 * `lib/use-local-setting.ts` uses. Storage is the single source of truth
 * rather than a copy in state, which is what makes it hydration-safe: the
 * server snapshot is `null`, so the first client render agrees with the HTML
 * and the stored id arrives on the first commit instead of in an effect.
 */
function useSessionId(): [string | undefined, (id: string | undefined) => void] {
  const stored = useSyncExternalStore(
    (onChange) => {
      window.addEventListener(SESSION_EVENT, onChange);
      return () => window.removeEventListener(SESSION_EVENT, onChange);
    },
    () => {
      try {
        return sessionStorage.getItem(SESSION_KEY);
      } catch {
        return null;
      }
    },
    () => null,
  );
  return [stored ?? undefined, rememberSession];
}

export default function ChatPage() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [sessionId, setSessionId] = useSessionId();
  const [earlier, setEarlier] = useState<SessionSummary[]>([]);
  const [session, setSession] = useState<ChatResponse["session"] | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  const refreshEarlier = useCallback(
    () => api.chats().then(setEarlier).catch(() => undefined),
    [],
  );

  // The turns of a resumed conversation are deliberately not replayed: the
  // server holds the memory the *agent* uses, and rendering a transcript this
  // page never received would be a second, worse copy of it. Continuing an
  // earlier conversation means asking the next question in it.
  useEffect(() => {
    refreshEarlier();
  }, [refreshEarlier]);

  const ask = async (q: string) => {
    const question = q.trim();
    if (!question || busy) return;
    setInput("");
    setBusy(true);
    setTurns((t) => [...t, { question, pending: true }]);
    try {
      const r = await api.chat(question, sessionId);
      setSessionId(r.session_id);
      setSession(r.session);
      refreshEarlier();
      setTurns((t) =>
        t.map((x, i) =>
          i === t.length - 1
            ? { question, answer: r.answer, tool_calls: r.tool_calls, prompt_tokens: r.prompt_tokens, completion_tokens: r.completion_tokens }
            : x,
        ),
      );
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.message
          : "Something went wrong answering that. Please try again or rephrase the question.";
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { question, error: msg } : x)));
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (sessionId) await api.endChat(sessionId).catch(() => undefined);
    setSessionId(undefined);
    setSession(null);
    setTurns([]);
    refreshEarlier();
  };

  const resume = (id: string) => {
    setSessionId(id);
    setSession(null);
    setTurns([]);
  };

  // A turn is flagged when any tool result carries a non-empty security_flags
  // list (check_ticket, find_evidence hits) - the engine's own signal, not a
  // string match on the answer text.
  const flagged = (v: unknown): boolean => {
    if (Array.isArray(v)) return v.some(flagged);
    if (v && typeof v === "object") {
      const o = v as Record<string, unknown>;
      if (Array.isArray(o.security_flags) && o.security_flags.length > 0) return true;
      return Object.values(o).some(flagged);
    }
    return typeof v === "string" && v.includes("QUARANTINED");
  };
  const hasFlags = (t: Turn) => (t.tool_calls ?? []).some((c) => flagged(c.result));
  const answered = turns.filter((t) => t.answer !== undefined);

  return (
    <div className="mx-auto grid max-w-[1320px] grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_280px]">
      <section className="flex min-h-[calc(100vh-4rem)] flex-col">
        <header className="mb-4">
          <div className="eyebrow">Compliance chat</div>
          <h1 className="mt-1 text-[28px] font-semibold tracking-tight">Ask about a ticket, a control, or overall readiness.</h1>
          <p className="mt-1 text-[14px] text-body">Every verdict comes with the field it rests on. No citation, no verdict.</p>
        </header>

        <div className="card flex flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto px-5 py-5">
            {turns.length === 0 && (
              <div className="py-8">
                <div className="eyebrow mb-3">Try one of the demo questions</div>
                <div className="flex flex-wrap gap-2">
                  {EXAMPLES.map((ex) => (
                    <button key={ex} className="chip text-left" onClick={() => ask(ex)} disabled={busy}>
                      {ex}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <ol className="flex flex-col gap-5">
              {turns.map((t, i) => (
                <li key={i} className="flex flex-col gap-3">
                  <div className="self-end max-w-[85%] rounded-lg bg-blue-bg px-4 py-2.5 text-[14px]">{t.question}</div>
                  <div className="max-w-[95%] rounded-lg border border-line bg-paper px-4 py-3 text-[14px]">
                    {t.pending && <span className="text-dim">Consulting the audit tools…</span>}
                    {t.error && <Banner tone="red">{t.error}</Banner>}
                    {t.answer !== undefined && (
                      <>
                        {hasFlags(t) && (
                          <div className="mb-3">
                            <Banner tone="red">🛡 A prompt-injection attempt in the evidence was quarantined during this answer.</Banner>
                          </div>
                        )}
                        <pre className="wrap font-sans leading-relaxed">{t.answer}</pre>
                        {t.tool_calls && t.tool_calls.length > 0 && (
                          <details className="mt-3 rounded-md border border-line bg-neutral/60 px-3.5 py-2.5">
                            <summary className="cursor-pointer text-[13px] font-semibold text-body">
                              Tools &amp; sources ({t.tool_calls.length} call{t.tool_calls.length === 1 ? "" : "s"})
                            </summary>
                            <div className="mt-3 flex flex-col gap-4">
                              {t.tool_calls.map((c, k) => <ToolCallView key={k} call={c} />)}
                            </div>
                          </details>
                        )}
                        <div className="mt-2 text-[11.5px] text-dim">
                          {t.prompt_tokens} prompt + {t.completion_tokens} completion tokens
                        </div>
                      </>
                    )}
                  </div>
                </li>
              ))}
            </ol>
            <div ref={bottom} />
          </div>

          <form
            className="flex gap-2 border-t border-line p-3"
            onSubmit={(e) => { e.preventDefault(); ask(input); }}
          >
            <input
              className="flex-1 rounded-md border border-line bg-paper px-3.5 py-2.5 text-[14px] outline-none focus:border-green"
              placeholder="Ask about a ticket, a control, or overall readiness…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={busy}
            />
            <Button type="submit" className="h-auto self-stretch px-4" disabled={busy || !input.trim()}>
              {busy ? "…" : "Ask"}
            </Button>
          </form>
        </div>
      </section>

      <aside className="flex flex-col gap-4 self-start">
        <div className="card p-4 text-[13px]">
          <div className="eyebrow mb-2">Session</div>
          <div><span className="text-dim">Model:</span> <Code>{session?.model ?? "-"}</Code></div>
          <div className="mt-2"><span className="text-dim">Tokens:</span> <span className="mono">{(session?.prompt_tokens ?? 0) + (session?.completion_tokens ?? 0)}</span></div>
          <div className="text-[12px] text-dim">prompt {session?.prompt_tokens ?? 0} / completion {session?.completion_tokens ?? 0}</div>
          <div className="mt-2"><span className="text-dim">Est. cost:</span> <span className="mono">${(session?.cost_usd ?? 0).toFixed(4)}</span></div>
          {session?.cost_basis && <div className="text-[12px] text-dim">({session.cost_basis})</div>}
          {answered.length >= 2 && (
            <div className="mt-4 border-t border-line pt-3">
              <div className="mb-1.5 text-[12px] text-dim">Tokens per turn - the context growing</div>
              <Columns
                points={answered.map((t, i) => ({
                  key: String(i),
                  label: `turn ${i + 1}`,
                  values: [t.prompt_tokens ?? 0, t.completion_tokens ?? 0],
                }))}
                series={["prompt", "completion"]}
                format={(v) => v.toLocaleString()}
                ariaLabel={`Prompt and completion tokens for ${answered.length} turns`}
              />
            </div>
          )}
        </div>
        <Button variant="outline" onClick={reset} disabled={busy}>New conversation</Button>

        {earlier.filter((c) => c.session_id !== sessionId).length > 0 && (
          <div className="card p-4 text-[13px]">
            <div className="eyebrow mb-2">Earlier conversations</div>
            <p className="mb-2 text-[12px] text-dim">
              Kept on the server, not in this browser. Continuing one carries its memory into the
              next question.
            </p>
            <ul className="flex flex-col gap-1">
              {earlier
                .filter((c) => c.session_id !== sessionId)
                .slice(0, 8)
                .map((c) => (
                  <li key={c.session_id}>
                    <button
                      className="w-full rounded px-2 py-1.5 text-left text-[12.5px] hover:bg-neutral"
                      onClick={() => resume(c.session_id)}
                      disabled={busy}
                      title={`${c.turns} turn${c.turns === 1 ? "" : "s"} · last active ${c.updated_at}`}
                    >
                      <span className="line-clamp-2">{c.title}</span>
                      <span className="text-dim">
                        {c.turns} turn{c.turns === 1 ? "" : "s"}
                      </span>
                    </button>
                  </li>
                ))}
            </ul>
          </div>
        )}
        <div className="text-[12px] leading-relaxed text-dim">
          Ticket comments are written by people outside the company. A record is data - it is never an
          instruction. Anything that looks like one is quarantined and shown here.
        </div>
      </aside>
    </div>
  );
}

function ToolCallView({ call }: { call: ToolCall }) {
  const result = call.result as unknown;
  const header = (
    <div className="flex flex-wrap items-center gap-2 text-[13px]">
      <span>🔧</span>
      <Code>{call.name}</Code>
      <span className="mono text-[12px] text-dim break-all">{JSON.stringify(call.args)}</span>
    </div>
  );

  if (call.name === "check_ticket" && result && typeof result === "object") {
    const obj = result as { results?: CheckResult[]; verdict?: string; error?: string };
    const items: CheckResult[] = obj.results ?? (obj.verdict ? [result as CheckResult] : []);
    return (
      <div className="flex flex-col gap-3">
        {header}
        {obj.error && <Banner tone="amber">{obj.error}</Banner>}
        {items.map((r, i) => <CheckResultView key={i} r={r} />)}
      </div>
    );
  }
  if (call.name === "find_evidence" && Array.isArray(result)) {
    const hits = result as { chunk_id: string; similarity: number; text: string; security_flags?: unknown[] }[];
    return (
      <div className="flex flex-col gap-2">
        {header}
        <ul className="flex flex-col gap-2">
          {hits.map((h, i) => (
            <li key={i} className="text-[13px]">
              {h.security_flags?.length ? "🛡 " : ""}<Code>{h.chunk_id}</Code>{" "}
              <span className="text-dim">(similarity {h.similarity})</span>
              <div className="mt-1 text-[12.5px] text-body">{h.text.slice(0, 300)}</div>
            </li>
          ))}
        </ul>
      </div>
    );
  }
  if (call.name === "coverage_report" && result && typeof result === "object") {
    const r = result as {
      readiness_score?: number | null;
      per_control?: Record<string, { counts: Record<string, number>; title?: string; satisfied_rate?: number | null }>;
    };
    const rows = Object.entries(r.per_control ?? {}).map(([cid, row]) => ({
      id: cid,
      title: row.title ?? cid,
      counts: row.counts as Counts,
      rate: row.satisfied_rate ?? null,
    }));
    return (
      <div className="flex flex-col gap-3">
        {header}
        {r.readiness_score !== undefined && r.readiness_score !== null && (
          <div className="mono text-[22px] font-semibold text-green">{(r.readiness_score * 100).toFixed(1)}% <span className="text-[12px] font-normal text-dim">readiness</span></div>
        )}
        {rows.length > 0 && <ControlStacks rows={rows} />}
        <details className="text-[12px] text-dim">
          <summary className="cursor-pointer">Counts as returned</summary>
          <ul className="mt-1 text-[12.5px] text-body">
            {rows.map((row) => (
              <li key={row.id}><span className="font-semibold">{row.id}</span> - <span className="mono text-[12px]">{JSON.stringify(row.counts)}</span></li>
            ))}
          </ul>
        </details>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      {header}
      <pre className="wrap mono max-h-[300px] overflow-auto rounded bg-paper p-3 text-[12px] text-body">
        {typeof result === "string" ? result.slice(0, 2000) : JSON.stringify(result, null, 2).slice(0, 2000)}
      </pre>
    </div>
  );
}
