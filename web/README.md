# web/ - the React front end (CR-8)

Next.js 16 (App Router, TypeScript, Tailwind v4, shadcn/ui on Radix). Talks only to `api/`; knows
nothing about Python.

```bash
# terminal 1 - the engine's HTTP boundary
uv run uvicorn api.main:app --reload --port 8000

# terminal 2 - this app
cd web
cp .env.local.example .env.local     # NEXT_PUBLIC_API_URL=http://localhost:8000
pnpm install
pnpm dev                              # http://localhost:3000
```

## Pages

| Route | What | Streamlit equivalent |
|---|---|---|
| `/` | **Audit report** - pack picker, scope, run with live progress (SSE), score with its denominator, per-control table, open gaps / review queue / all pairs, agent trace per pair, Markdown export | `audit/report_view.py` |
| `/chat` | **Compliance chat** - the v1 agent as one panel: answer, tools & sources, verdict badges, citations, quarantine banner, RAG trail, session cost. The four `docs/DEMO.md` prompts are one-click chips. | `assistant/app.py` |
| `/records` | The evidence under audit, read as the judge reads it | - |

## The one rule

**No business logic in the browser.** Every number on screen came out of
`api/`, which in turn holds none either - `run_batch`, `AuditAgent`,
`load_pack`, `load_tickets` do the work. This app decides layout, not verdicts.
The two places it does arithmetic - the `2n-3n` call estimate and turning a
0-1 rate into a percentage - are the same presentation math the Streamlit
sidebar does.

## Layout

```
lib/schema.d.ts     GENERATED from ../api/openapi.json - `pnpm gen:types`; never edit
lib/api.ts          the typed client - the only file that knows a URL; types alias schema.d.ts
components/nav.tsx  shell, API health probe
components/ui.tsx   VerdictBadge, ScoreLedger, RateBar, Banner, CheckResultView (ours)
components/ui/      shadcn/ui primitives, generated - select, tabs, button, badge, tooltip
components.json     shadcn config (style radix-nova, Tailwind v4 css-variables)
lib/utils.ts        `cn()` - shadcn's class merger
app/layout.tsx      IBM Plex via next/font, sidebar + main
app/globals.css     design tokens - same palette as marketing/deck; shadcn's semantic
                    variables (--primary, --ring, --border …) are bound to them
app/page.tsx        report
app/chat/page.tsx   chat
app/records/page.tsx
```

Design tokens match the presentation deck on purpose: the product and the
slides read as one thing. shadcn/ui (added 2026-09-08) supplies accessible
primitives - the pack picker, the report tabs, every button - and nothing
about the look: its `:root` variables point at our tokens, so `--primary` is
the deck green and there is no Geist, no zinc, no dark theme. Add a primitive
with `pnpm dlx shadcn@latest add <name>`; it lands in `components/ui/` already
themed. Our own grey-text token is `--color-dim` (`text-dim`), because shadcn
uses `muted` for a surface colour and the two must not collide.

## Checks

```bash
pnpm gen:types           # regenerate lib/schema.d.ts after the API changes
pnpm exec tsc --noEmit   # types - fails if the API contract moved under us
pnpm lint                # eslint
pnpm build               # the real test: Turbopack + TS + prerender
```

## Docker

`web/Dockerfile` builds Next's standalone output into a ~150 MB image; `compose.yaml`
at the repo root runs it as `web` beside `api`. `NEXT_PUBLIC_API_URL` is inlined at
*build* time and is where the **browser** reaches the API - the host's published
port by default, your public URL when deploying behind a hostname:

```bash
docker compose build --build-arg NEXT_PUBLIC_API_URL=https://audit.example.com/api web
```

`AGENTS.md` / `CLAUDE.md` in this directory are written by `next dev` and point
at the bundled Next 16 docs; leave them.
