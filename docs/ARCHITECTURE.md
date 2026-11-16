# Audit-Readiness Assistant — Project Brief & Build Plan

*Working name: **AuditReady** (placeholder — see naming note at end)*
*Gaurav Thorat*

---

## 1. One-line pitch

An AI system that reads an organisation's project records (tickets, emails, documents), checks them against a compliance checklist, and produces an **audit-readiness report** — showing which controls are satisfied, which have gaps, and drafting the missing evidence, with **every finding cited back to the real source artifact**. Human verifies and signs.

First checklist: **ISO 9001**. The engine is checklist-agnostic, so ISO 27001, ISO 20000, and CMMI are later config packs, not rewrites.

---

## 2. The problem (the story that sells it)

At an ISO-certified IT services company where I worked, an external ISO auditor spent a week checking whether every service ticket had complete history, logged client communication, comments, and sign-off — and whether records and backups were in place. Developers lost days confirming all of this manually. This repeats at every ISO-certified services company, at every annual surveillance audit and every 3-year recertification.

Audit prep today = spreadsheets + manual record review + consultants. Slow, expensive, error-prone. The pain is real and recurring.

## 3. Positioning — why this isn't a reinvented wheel

Existing tools (Jama, DOORS, codeBeamer; Vanta/Drata/Secureframe for security evidence) are **systems of record** — they store artifacts and enforce structure. The human still does all the *reasoning*: reading the criterion, deciding it applies, judging whether evidence is sufficient, spotting the gap.

This project is a **system of reasoning**: it *reads and judges* the evidence and tells you "ticket 4471 will fail the audit — no client sign-off logged before closure — here's a drafted remediation note." That reasoning layer, grounded and measurable, is the part the incumbents leave to humans. That's the wedge.

---

## 4. What it does (end to end)

1. **Ingest** ticket/email/document data (Jira, ServiceNow, email, Confluence).
2. **Index** it into a vector store alongside the loaded checklist criteria.
3. For **each control** in the checklist, run a reasoning loop: retrieve candidate evidence → judge sufficiency → draft summary OR flag gap → self-verify the citation.
4. **Output** an audit-readiness report: satisfied vs gap per control, the specific missing evidence, a drafted remediation note, and a citation to the real artifact for every claim.
5. **Human verifies and signs** — the tool drafts, it never certifies.

---

## 5. Architecture & where each technology lives

| Layer | Technology | Job |
|---|---|---|
| Ingestion | Connectors + chunking | Pull records from Jira/ServiceNow/email/docs; chunk for embedding |
| Vector store | **pgvector** (Postgres — your stack) | Holds embedded evidence **and** embedded checklist criteria; semantic search |
| Retrieval | **RAG** (hybrid search + rerank) | For each control, pull the criterion + candidate evidence — grounds every verdict in real data |
| Orchestration | **LangGraph** | The per-control state machine: retrieve → judge → draft/flag → self-verify citation. Multi-step + branching = why a framework earns its place here |
| Reasoning | **LLM** (bring-your-own endpoint) | The judge and draft steps. BYO endpoint (e.g. their Azure OpenAI) so data never leaves their boundary |
| Guardrails | Citation enforcement + injection defense | "No citation, no output." Ingested tickets/emails are **untrusted input** → your prompt-injection defense lives here |
| Memory | **Long-term memory** | Org process conventions + prior audit findings persist across runs; run 2 is smarter than run 1 |
| Evaluation | **Eval harness** | Measures the whole thing: gap-detection recall, clause-match accuracy, citation validity. Turns "plausible text" into "catches 84% of gaps" |

**Every item is load-bearing — none bolted on to look busy.** That's the bar this had to clear, and it does.

---

## 6. The config-driven core: checklist packs

The agent never hardcodes a standard. A "standard" is a **loadable pack** of controls. This is the single most important design decision — it kills the copyright problem (users bring their own licensed criteria), handles per-org interpretation of ISO, and makes "supports multiple standards" a config file instead of a rewrite.

### Checklist-item schema (worked ISO 9001 example, YAML)

```yaml
control_id: "ISO9001-8.2.1-client-comm"
standard: "ISO 9001:2015"
clause: "8.2.1"
title: "Customer communication"
description: >
  Evidence that the customer was informed of, and agreed to,
  the resolution before the request was closed.
expected_evidence:                # what artifact types satisfy this
  - ticket_resolution_comment
  - client_email_thread
  - approval_or_signoff_record
sufficiency_rubric: >             # what "good enough" means — BE SPECIFIC
  Satisfied only if: (a) a resolution comment exists AND
  (b) at least one logged client communication precedes closure AND
  (c) there is explicit client acknowledgement or sign-off.
  A resolution comment alone = PARTIAL. No client comms = GAP.
citation_required: true           # verdict must point to specific artifact(s)
severity: "major"                 # weighting for the report
```

A quality manager can author these in a form or YAML file — no code. **Version the packs** (auditor and tool both need to know which criteria version a verdict used).

**Scope discipline — draw the line here:** only *record-based, reasoning-checkable* criteria belong in the engine (tickets, emails, docs). Facts like "confirm nightly backups ran" are infra checks, not reasoning — list them as manual checklist items, don't try to make the LLM do them. Code comments are out (ISO 9001 doesn't care about them).

---

## 7. Phased build plan

**Phase 1 — Walking skeleton (get it end-to-end and ugly).**
Ingest a handful of sample tickets (synthetic JSON is fine) → check them against **one** hardcoded control → one LLM call → print a plain-text verdict. No vector DB, no LangGraph yet. Goal: close the loop, kill the risk of "does this even work."

**Phase 2 — Build the eval harness BEFORE improving quality.**
Create a labelled dataset: sample tickets, some with deliberately injected deficiencies (missing sign-off, no client comms), some clean. Define metrics: gap-detection recall, false-positive rate on clean tickets, citation validity. This counterintuitive ordering — measure first — is the senior move. Everything after this is measured.

**Phase 3 — Add the real stack, each justified by a metric moving.**
- pgvector + embeddings; move retrieval to real RAG (hybrid + rerank).
- LangGraph: refactor the check into the retrieve → judge → draft/flag → self-verify graph.
- Guardrails: citation enforcement + injection defense on ingested content.
- Config-driven checklist packs (load ISO 9001 as YAML, not code).

**Phase 4 — Depth & polish.**
- Long-term memory (org conventions, prior findings).
- BYO-LLM-endpoint config; Dockerise for self-hosting.
- A second checklist pack (ISO 20000 or 27001) to *prove* the config-driven design.
- Report UI + a 2-minute demo: messy ticket export in → audit-readiness verdict out.

---

## 8. Evaluation strategy (the differentiator)

Nobody using an off-the-shelf tool can tell you their gap-detection accuracy. You can. Headline metrics:
- **Gap-detection recall** — of known deficiencies, how many caught?
- **False-positive rate** — flags on clean records (the metric that decides if people trust it).
- **Citation validity** — does every cited artifact actually exist and support the claim?
- **Clause-match accuracy** — right control matched to right evidence?

Wire the eval to run in CI so a prompt/model change that regresses quality **fails the build**. Prompts as versioned, tested artifacts = the most senior thing in the project.

---

## 9. Deployment model

Compliance data is sensitive, so the model companies accept is **self-hosted / in-their-own-cloud**, not SaaS.
- Ship as a **Docker** container they run in their own infra (you know Docker/Portainer).
- Connect to their Jira/ServiceNow via *their* credentials.
- **Bring-your-own LLM endpoint** (their Azure OpenAI tenant or a local model) → no record data leaves their boundary.

"Deploys in your VPC, your data never leaves, bring your own model" is exactly what a compliance buyer wants — and designing for it is itself a senior signal. For v2 demo, a simple hosted version on synthetic tickets is fine.

## 10. Licensing

**Apache License 2.0** (see `LICENSE` and `NOTICE`). The repository is public so
employers, reviewers and contributors can read and run the full stack — engine,
agents, connectors, eval harness and self-hosted deployment — without requesting
access.

A compliance buyer's real question remains "does our data leave our network",
and the answer to that is the self-hosted deployment (§9), not the licence.
`NOTICE` records that checklist packs are own-worded paraphrases (not ISO text)
and that the corpus is synthetic; the product drafts findings and never certifies.

---

## 11. Extensibility roadmap (later packs)

Same engine, new YAML packs — no rewrites:
- **ISO/IEC 20000** (IT service management) — maps most directly to ticket/incident/change data; closest to the IT-services scenario in §2.
- **ISO/IEC 27001** (information security) — record-heavy, huge demand, Vanta/Drata prove the market.
- **CMMI** — process-maturity; big in Indian IT services. Fits the process-area → practice → evidence shape. **Caveat:** flag evidence gaps only; never claim to assign a maturity level (that's an appraiser's call).

Copyright note for all: ship the engine + sample/synthetic packs; users bring their own licensed criteria text.

---

## 12. Risks & how they're handled

| Risk | Handling |
|---|---|
| Fuzzy "is this evidence sufficient" judgment | Specific sufficiency rubrics per control + eval harness measures judgment accuracy |
| "Just another compliance tool" reaction | Front-load the "system of reasoning vs record" framing + the eval numbers |
| Standards are copyrighted | Bring-your-own-criteria; ship engine + synthetic packs only |
| A previous employer operates in this space | Stay scrupulously clean of any proprietary data or designs from any previous employer — build from public standard structure and general domain knowledge only |
| Scope creep ("check everything") | Record-based reasoning-checkable criteria only; infra checks (backups) are manual checklist items |

---

## 13. Suggested tech stack

- **Language:** Python (where the AI ecosystem lives)
- **Orchestration:** LangGraph
- **Vector store:** Postgres + pgvector
- **LLM:** provider-agnostic via config (OpenAI/Azure OpenAI/local)
- **API/UI:** FastAPI backend + a light React front end (plays to your full-stack strength)
- **Packaging:** Docker
- **Eval:** custom harness + CI (GitHub Actions — your stack)

---

## Naming note

*AuditReady* is a placeholder. Alternatives to consider: **Attest**, **ClauseCheck**, **EvidenceEngine**, **AuditLens**. Pick something that says "reasoning/evidence," not "storage."

---

*Next concrete step: Phase 1 walking skeleton — sample tickets in, one control checked, verdict out.*
