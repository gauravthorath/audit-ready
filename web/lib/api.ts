/**
 * The typed client for api/ — every shape here mirrors a Python dict one-to-one.
 *
 * This file is the only place the browser knows a URL. Components import
 * functions, never fetch; if the wire changes, it changes here and the
 * compiler finds every caller.
 */

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  /** The correlation id of the failed call — the same id the API logged under. */
  requestId: string;
  constructor(status: number, detail: string, requestId: string) {
    // The reference goes into the message itself so every place that already
    // shows `e.message` shows the id a support person can grep the logs for.
    super(`${detail} (ref ${requestId})`);
    this.status = status;
    this.requestId = requestId;
  }
}

/**
 * One id per call, born here and carried to the model call on the server:
 * the API binds it to every log line of the request (and of an audit run it
 * starts) and echoes it back in the same header.
 */
export const REQUEST_ID_HEADER = "X-Request-ID";

function newRequestId(): string {
  try {
    return crypto.randomUUID().replace(/-/g, "").slice(0, 16);
  } catch {
    return Math.random().toString(16).slice(2, 18);
  }
}

// ---------------------------------------------------------------------------
// Shapes — generated from api/openapi.json (see `pnpm gen:types`), aliased here
// so components import stable names. If a Python response model changes and the
// JSON is re-exported, `tsc` fails at every site that assumed the old shape.
// That is the point: one contract, and drift is a compile error, not a bug
// report.
// ---------------------------------------------------------------------------

import { announceUnauthorized, getToken } from "./auth";

import type { components } from "./schema";

type S = components["schemas"];

export type Control = S["ControlOut"];
export type Pack = S["PackOut"];
export type RecordSummary = S["RecordSummaryOut"];
export type Comment = S["CommentOut"];
export type Ticket = S["TicketOut"];
export type Health = S["HealthOut"];
export type Pricing = S["PricingOut"];
export type Spend = S["SpendOut"];
export type ToolCall = S["ToolCallOut"];
export type ChatResponse = S["ChatOut"];
export type SessionSummary = S["SessionSummaryOut"];
export type StartAudit = S["StartAuditOut"];
export type Progress = S["ProgressOut"];
export type Verification = S["VerificationOut"];
export type PairRow = S["PairRowOut"];
export type PerControl = S["PerControlOut"];
export type Report = S["ReportOut"];
export type RunSnapshot = S["RunOut"];

export type Severity = Control["severity"];
export type PairStatus = PairRow["status"];
export type Verdict = "satisfied" | "partial" | "gap" | "not_applicable";

// Not HTTP responses, so not in the OpenAPI document: the shape of one
// `check_ticket` tool result inside `ChatResponse.tool_calls[].result`
// (core/tools.check_ticket), which a PairRow is a superset of.
export type SecurityFlag = { path: string; [k: string]: unknown };

export type CheckResult = {
  ticket_id: string;
  control_id: string;
  control_title?: string | null;
  severity?: string | null;
  verdict?: string | null;
  rationale?: string | null;
  citations?: string[];
  citation_check?: { status?: string; note?: string; [k: string]: unknown };
  security_flags?: SecurityFlag[] | { [k: string]: unknown }[];
  evidence_queries?: string[];
  evidence_chunks?: string[];
  prompt_tokens?: number;
  completion_tokens?: number;
};

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const requestId = newRequestId();
  let res: Response;
  try {
    // The id header makes every call "non-simple", so the browser preflights
    // each URL once; the API answers with max-age=600 and the browser caches
    // it. Only bodies need a Content-Type.
    const token = getToken();
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: {
        [REQUEST_ID_HEADER]: requestId,
        // Sent whenever we have one. An open deployment ignores it; a locked
        // one answers 401 without it, and the panel goes up.
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError(0, `Cannot reach the API at ${API_URL}. Is uvicorn running?`, requestId);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail) detail = JSON.stringify(body.detail);
    } catch {
      /* not JSON — keep statusText */
    }
    const ref = res.headers.get(REQUEST_ID_HEADER) ?? requestId;
    console.error(`[auditready] ${init?.method ?? "GET"} ${path} -> ${res.status} (ref ${ref})`);
    if (res.status === 401) announceUnauthorized();
    throw new ApiError(res.status, detail, ref);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => request<Health>("/health"),
  spend: () => request<Spend>("/spend"),
  packs: () => request<Pack[]>("/packs"),
  records: () => request<RecordSummary[]>("/records"),
  record: (id: string) => request<Ticket>(`/records/${encodeURIComponent(id)}`),

  startAudit: (body: { pack_id: string; ticket_ids?: string[]; control_ids?: string[] }) =>
    request<StartAudit>(
      "/audit",
      { method: "POST", body: JSON.stringify(body) },
    ),
  runs: () => request<RunSnapshot[]>("/audit"),
  run: (id: string) => request<RunSnapshot>(`/audit/${id}`),
  reportMarkdownUrl: (id: string) => `${API_URL}/audit/${id}/report.md`,

  chats: () => request<SessionSummary[]>("/chat"),
  chat: (question: string, session_id?: string) =>
    request<ChatResponse>("/chat", {
      method: "POST",
      body: JSON.stringify({ question, session_id }),
    }),
  endChat: (session_id: string) =>
    request<{ ended: boolean }>(`/chat/${session_id}`, { method: "DELETE" }),
};

/**
 * Subscribe to a run's progress. Returns an unsubscribe.
 *
 * The API ends the stream itself after the terminal event, so `onEnd` fires
 * exactly once; a dropped connection surfaces through `onError` and the caller
 * falls back to polling `api.run`.
 */
export function streamRun(
  run_id: string,
  handlers: {
    onProgress: (p: { status: RunSnapshot["status"]; progress: Progress; error: string | null }) => void;
    onEnd: (status: RunSnapshot["status"]) => void;
    onError?: () => void;
  },
): () => void {
  // EventSource cannot set a header, so a locked deployment could not stream
  // progress at all. The caller falls back to polling `api.run` on error,
  // which does carry the credential — the progress bar stutters, the run does
  // not break. (Passing the token in the URL would put a credential in every
  // access log; that is worse than a slower bar.)
  const es = new EventSource(`${API_URL}/audit/${run_id}/events`);
  es.addEventListener("progress", (e) => {
    handlers.onProgress(JSON.parse((e as MessageEvent).data));
  });
  es.addEventListener("end", (e) => {
    const { status } = JSON.parse((e as MessageEvent).data) as { status: RunSnapshot["status"] };
    es.close();
    handlers.onEnd(status);
  });
  es.onerror = () => {
    es.close();
    handlers.onError?.();
  };
  return () => es.close();
}
