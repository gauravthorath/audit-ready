# core/

Shared, framework-agnostic building blocks. Built once, reused by both
`assistant/` and `audit/`. **Nothing here imports LangChain or LangGraph**
(REQUIREMENTS.md FR-7 / NFR-3) — that rule is what lets v2 reuse
this package unchanged.

Modules (all built, each with a smoke test in `scripts/`):

| Module | What it does | Key requirement |
|---|---|---|
| `llm.py` | The ONLY module that talks to the LLM provider (OpenRouter via the OpenAI SDK; BYO endpoint = env change). Retries, single `LLMError`, token accounting. | NFR-1 |
| `checklists.py` | Loads + validates YAML checklist packs into Pydantic models (`Control`, `ChecklistPack`). Config-as-data: a new standard is a new YAML file. | NFR-4 |
| `judge.py` | The judging heart: one (record, control) pair → LLM → validated `satisfied / partial / gap / not_applicable` verdict with rationale + citations. | FR-6 |
| `retrieval.py` | Ingestion (natural-unit chunking), local sentence-transformers embeddings, metadata-filtered semantic search. Dispatches to Chroma (default) or pgvector on `AUDITREADY_VECTOR_STORE`; `store_is_empty()` / `store_label()` for the surfaces. | FR-1, FR-2 |
| `pgvector_store.py` | Postgres + `vector` extension backend: one table namespaced by store, HNSW cosine index, Chroma's `where` dialect translated to parameterised SQL, transactional rebuild. `AUDITREADY_PG_DSN`. | FR-1, CR-4 |
| `rag.py` | Advanced RAG: decomposes a control's rubric into focused evidence sub-queries (cached per control), multi-query search, merged `EvidenceBundle`. | FR-3 |
| `tools.py` | The three domain tools as plain functions: `find_evidence`, `check_ticket`, `coverage_report`. | FR-5–7 |
| `guardrails.py` | Input validation, injection pattern-scan + quarantine, citation resolution checks. | FR-10, 13, 14 |
| `tracing.py` | Correlation ids and structured logs, stdlib only: a contextvar bag (`bind`), `log_event` with fields, JSON and text formatters that append the bag, `span` timing. One id from the browser to every `llm.call`. | NFR-5, NFR-6 |
| `spend.py` | The spend ledger (append-only JSONL, per-actor and deployment daily totals, UTC days) and the budget arithmetic the API refuses with. | FR-17 |

Not built (deliberate): hybrid BM25 search (FR-4 stretch — metadata filtering
covers the exact-id need at this scale), `export_report` (the UI exports
conversations; batch report export is CR-2).
