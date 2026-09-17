# Requirements Document — AuditReady Compliance Assistant (v1)

*Gaurav Thorat*
*Working name: AuditReady · Scope: v1 chatbot (foundation for v2)*

---

## 1. Purpose

Define the requirements for a domain-specialised **compliance-assistant chatbot** that answers ISO 9001 audit-readiness questions over a project's records, using advanced RAG and tool calling. The build must satisfy the v1 rubric **and** be structured as the reusable core of the later v2.

## 2. Scope

**In scope (v1):** a conversational Streamlit chatbot over a *static* knowledge base (ISO 9001 checklist + a synthetic ticket/email corpus), advanced RAG, 3–4 domain tools, source citations, prompt-injection defense, input validation, error handling, and a RAGAs evaluation.

**Out of scope (deferred to v2 — see Appendix A):** live connectors (Jira/ServiceNow/Outlook), the LangGraph autonomous per-control agent, batch report generation, self-hosted/VPC deployment, BYO-LLM endpoint, multi-standard packs, long-term memory.

## 3. Users & context

- **Primary user:** a compliance/quality manager or engineer preparing for an ISO 9001 audit.
- **Interaction:** asks questions ("Is ticket 4471 audit-ready?", "Which controls are we failing?", "Draft remediation for the customer-communication gap") and receives cited, context-aware answers.
- **Estimated effort:** 20–25 hours (v1 budget).

---

## 4. Functional requirements

### 4.1 Knowledge base & RAG (core)
- **FR-1** The system SHALL maintain a domain knowledge base: the ISO 9001 checklist/criteria and a synthetic corpus of service tickets and emails.
- **FR-2** The system SHALL chunk source documents and store embeddings in a vector store, retrieving by similarity search.
- **FR-3** The system SHALL implement **advanced RAG with query translation** (e.g. multi-query / decomposition), justified by the fact that one control has several evidence requirements.
- **FR-4** *(Bonus — hard: hybrid search)* The system SHOULD combine semantic and keyword search to improve retrieval precision on clause/ID matches.

### 4.2 Tool calling (core — ≥3 tools)
- **FR-5** The system SHALL implement at least three domain tools, callable by the LLM:
  - **FR-5.1** `find_evidence(query)` — retrieves candidate evidence from the ticket/email corpus.
  - **FR-5.2** `check_ticket(ticket_id, control_id)` — judges a ticket against a control's sufficiency rubric and returns satisfied / partial / gap + rationale.
  - **FR-5.3** `coverage_report()` — computes the percentage of controls satisfied and lists open gaps.
- **FR-6** *(Optional 4th)* `export_report(format)` — exports the current findings (JSON/CSV/PDF).
- **FR-7** Tool functions SHALL be implemented as **framework-agnostic Python functions** (not embedded in LangChain classes) so v2's agent can reuse them unchanged. *(Architecture-critical for reuse.)*

### 4.3 Conversation
- **FR-8** The system SHALL support multi-turn conversation with context retained across turns within a session.
- **FR-9** *(Bonus — easy)* The system SHOULD allow exporting conversation history (JSON/CSV/PDF).

### 4.4 Citations & transparency
- **FR-10** *(Bonus — easy)* Every factual claim in a response SHALL cite its source (control clause and/or specific ticket/artifact). Uncited compliance claims are not permitted.
- **FR-11** The UI SHALL display the retrieved context/sources used for each answer.
- **FR-12** *(Bonus — easy)* The UI SHOULD visualise the RAG process (query → retrieval → answer).

### 4.5 Security
- **FR-13** *(Bonus — medium)* The system SHALL defend against prompt injection from untrusted corpus content (input-length cap, injection scan before token spend, system-prompt hardening, delimiter-fenced untrusted text). *(Reuses interview-app approach.)*
- **FR-14** The system SHALL validate and sanitise user input.

### 4.6 Reliability & operations
- **FR-15** The system SHALL implement error handling that fails gracefully (no crashes on API/LLM/parse errors) with clear user-facing messages.
- **FR-16** The UI SHALL show progress indicators for long operations (retrieval, tool runs).
- **FR-17** *(Bonus — medium)* The system SHOULD calculate and display token usage and cost per interaction.

### 4.7 Evaluation
- **FR-18** *(Bonus — hard)* The system SHALL include an evaluation of the RAG pipeline (RAGAs or equivalent) reporting retrieval and answer-quality metrics against a labelled question set. Extend with compliance-specific metrics: gap-detection recall and citation validity.

### 4.8 User interface
- **FR-19** The system SHALL provide an intuitive chat UI in **Streamlit** (Python track).
- **FR-20** The UI SHALL display tool-call results in a readable form.

---

## 5. Non-functional requirements

- **NFR-1 (LLM integration)** All LLM calls SHALL route through **OpenRouter** via the OpenAI-compatible SDK, isolated in a single client module (so v2 can swap to a BYO endpoint by config).
- **NFR-2 (Framework)** v1 SHALL use **LangChain** for orchestration and tool calling. *(LangGraph agent is v2 scope.)*
- **NFR-3 (Reusability)** Core logic (retrieval, tools, guardrails, checklist loading) SHALL be modular and framework-agnostic so v2 reuses it without rewrite.
- **NFR-4 (Configurability)** Checklist criteria SHALL be loaded from config (YAML), not hardcoded, to support future standards.
- **NFR-5 (Performance)** Typical answer latency SHOULD stay within interactive range; long tool runs must show progress (FR-16).
- **NFR-6 (Maintainability)** Code SHALL follow clean organisation (separated modules: ingestion, retrieval, tools, guardrails, LLM client, UI) — a graded criterion.
- **NFR-7 (Data)** No real customer or employer-proprietary data; synthetic corpus only.

---

## 6. Data requirements

- **DR-1** A synthetic ticket/email corpus with a mix of audit-ready and deficient records (missing sign-off, no client communication) — doubles as evaluation ground truth.
- **DR-2** An ISO 9001 checklist as config: each control with description, expected evidence types, sufficiency rubric, citation requirement, severity. (No redistribution of copyrighted ISO text — use own-worded criteria / synthetic pack.)
- **DR-3** A labelled question/expected-answer set for FR-18 evaluation.

---

## 7. Targeted bonus tasks (for maximum marks)

Rubric wants ≥2 medium + 1 hard. Planned:
- **Medium 1:** Prompt-injection protection (FR-13).
- **Medium 2:** Token usage & cost display (FR-17).
- **Hard 1:** RAG evaluation via RAGAs (FR-18).
- **Free easy wins:** citations (FR-10), RAG-process visualisation (FR-12), conversation export (FR-9).
- **Stretch hard (if time):** hybrid search (FR-4).

---

## 8. Acceptance criteria

| # | Acceptance test | Rubric criterion |
|---|---|---|
| AC-1 | User can chat and receive correct, domain-relevant answers | "project works as intended" |
| AC-2 | ≥3 tools demonstrably fire and return results in-chat | Tool calling |
| AC-3 | Knowledge base is domain-focused and drives answers | "relevant knowledge base" |
| AC-4 | Advanced RAG (query translation) is present and explained | Advanced RAG |
| AC-5 | Injection attempt in a ticket does not hijack the assistant | Security considerations |
| AC-6 | Errors/edge cases handled without crashing | Error scenarios/edge cases |
| AC-7 | Responses cite sources; context shown in UI | Bonus / transparency |
| AC-8 | RAGAs evaluation runs and reports metrics | Bonus hard task |
| AC-9 | Code is modular and cleanly organised | Code organisation |
| AC-10 | Learner can explain limitations + improvements | Reflection |

---

## 9. Assumptions & dependencies

- **AS-1** An OpenRouter API key, or any OpenAI-compatible endpoint, is available.
- **AS-2** v1 is the foundation v2 builds on: shared core, no rewrite.
- **DP-1** LangChain, RAGAs, a vector store (Chroma for v1 simplicity; pgvector in v2), Streamlit.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Scope creep beyond 20–25h | Hard-freeze out-of-scope items (§2) to v2 |
| Fuzzy "sufficiency" judgments | Specific per-control rubrics (DR-2); measure with FR-18 |
| Over-coupling to LangChain | FR-7 / NFR-3: framework-agnostic core |
| V2 seen as "same project twice" | V2 must foreground new layers (Appendix A) |

---

## Appendix A — V2 requirements delta (out of scope for v1)

V2 reuses the v1 core and ADDS:
- **CR-1** LangGraph autonomous per-control agent (retrieve → judge → draft/flag → self-verify).
- **CR-2** Batch audit-readiness report generation (not just chat).
- **CR-3** Connector interface + ≥1 live connector (Jira / GitHub Issues) + file import (CSV/JSON/.eml); others stubbed.
- **CR-4** Self-hosted deployment (Docker) with BYO LLM endpoint; data stays in customer boundary.
- **CR-5** Multi-standard checklist packs (ISO 27001 / 20000 / CMMI) via config.
- **CR-6** Long-term memory (org conventions, prior findings).
- **CR-7** Evaluation at scale + CI gate (regression-testing prompts).
- **CR-8** React dashboard with the v1 chatbot as one panel alongside the report engine.

---

*Companion document: `audit-readiness-assistant-plan.md` (full v2 plan & architecture).*
