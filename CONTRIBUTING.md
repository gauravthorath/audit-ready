# Contributing to AuditReady

Thanks for taking an interest. This project is a grounded, cited compliance
audit-readiness engine. Small, well-scoped changes are welcome; large new
features should be discussed in an issue first.

## Ground rules

- **Synthetic data only.** Do not commit real client, employer, or personal
  records. The corpus under `assistant/data/corpus/` is fictional on purpose.
- **No copyrighted standard text.** Checklist criteria are own-worded
  paraphrases. Users bring licensed criteria as YAML packs.
- **`core/` stays framework-agnostic.** No LangChain / LangGraph imports under
  `core/`. Verify with `grep -r langchain core/` before committing there.
- **No citation, no verdict.** Changes that weaken citation checks or
  temperature-0 judging are bugs, not features.
- Be respectful in issues and PRs. Harassment or abuse is not accepted.

## Layout (where to put code)

| Path | Role |
|------|------|
| `core/` | Engine: retrieval, RAG, judge, tools, guardrails, LLM client |
| `assistant/` | LangChain chat agent + Streamlit |
| `audit/` | LangGraph per-control agent, batch report, evidence connectors |
| `api/` | FastAPI wire shape only — no verdict logic |
| `web/` | Next.js front end; talks only to `api/` |
| `tests/` | Offline pytest (no API key) |

Prompts live in `core/prompts.py`, `assistant/prompts.py`, or `audit/prompts.py`
— not inline in logic modules. LLM calls go through `core/llm.chat()`.

## Setup

```bash
uv sync
cp .env.example .env   # add an LLM key only for live smokes
uv run pytest          # offline unit suite
```

Front end (optional):

```bash
cd web && cp .env.local.example .env.local && pnpm install && pnpm dev
```

Live smokes that hit a real LLM cost money — use `uv run python scripts/smoke_<name>.py`
only when you mean to. Changing a rubric, judge prompt, model, or retrieval path
means re-running the eval and updating committed results (see `assistant/README.md`).

## Pull requests

1. Open an issue for anything larger than a small fix, or ask in the PR why the
   change is needed.
2. Keep PRs focused; one concern per PR when possible.
3. `uv run pytest` must pass. If you touch OpenAPI response models, also run
   `uv run python scripts/export_openapi.py` and `cd web && pnpm gen:types`.
4. Do not commit `.env`, `.chroma/`, `LEARNING.md`, or `reports/`.

## Security

Please do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
