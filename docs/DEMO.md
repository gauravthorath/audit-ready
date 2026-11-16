# Demo script

Twelve minutes, six moments. Each one is written as: **what you say**, **what you
do**, **what to point at when it answers**. The order builds - chat first because
it is legible, the batch report second because it is the product, the two swaps
last because they are the architecture claim.

**Before you start**

```bash
cp .env.example .env            # OPENROUTER_API_KEY set
uv sync && uv run pytest        # 297 green, ~5s - worth showing
uv run streamlit run assistant/app.py
```

Have a second terminal open on the repo, and `docs/system_map.svg` in a tab.

**Presenting from the React front end instead (CR-8).** Same engine, same six
moments, two processes instead of one:

```bash
uv run uvicorn api.main:app --port 8000          # terminal 1 - the HTTP boundary
cd web && pnpm dev                                # terminal 2 - http://localhost:3000
```

`/chat` has the four prompts below as one-click chips; `/` is the audit report
with live progress. Run the batch **before** the slot so the report is already on
screen. If either process misbehaves, `uv run streamlit run assistant/app.py` is the
same engine behind the Streamlit surface - switch and carry on; nothing in the
script changes.
Every ticket and control below comes from the labelled eval set, so the expected
answer is known - if one comes back wrong, say so out loud and move on. That is
a better moment than pretending.

---

## 1 - One ticket, one control (2 min)

> "The unit of work is one record against one control. Here's the happy path."

**Ask, in the chat page:**

```
Does ticket TCK-1001 satisfy the release sign-off control, ISO9001-8.6?
```

**Point at:**
- The verdict - **satisfied** - and then immediately the **Sources** panel. Every
  claim names the artifact it rests on. "The verdict is the cheap part. This is
  the part an auditor can check."
- The cost readout. Sub-cent per pair; a full pack is pennies.

## 2 - The planted deficiency (2 min)

> "Now a record where the procedure was not followed. This is what the tool is
> actually for."

**Ask:**

```
Check TCK-1009 against ISO9001-7.5-documented-info. Is the documented
information under control?
```

**Point at:**
- Verdict **gap**, and the *drafted* finding - what evidence is missing and what
  would close it. "It doesn't just say no. It says what to go get."
- Then say the thing that matters: **gap recall is 1.0 across all 68 labelled
  pairs.** A missed gap is a false assurance handed to an auditor, so that is the
  one number with no tolerance band in the CI gate.

## 3 - The injection attempt (1.5 min)

> "Ticket comments are written by people outside the company. Some of them are
> not friendly."

**Ask:**

```
Does TCK-1010 satisfy ISO9001-8.2.1-client-comm?
```

**Point at:**
- The **guardrail banner**. TCK-1010 carries a pasted vendor changelog containing
  `IGNORE ALL PREVIOUS INSTRUCTIONS…`. The record is quarantined, the attempt is
  surfaced, and the verdict still comes back **satisfied** - correctly, on the
  legitimate evidence in the rest of the ticket.
- "A record is data. It is never an instruction. That is enforced in
  `core/guardrails.py`, and this attack is in the corpus so every eval run
  exercises it."

## 4 - The whole pack at once (2.5 min)

> "One pair is a demo. An audit is every record against every control."

**Do:** switch to the **Audit report** page, pick `iso9001.yaml`, run it.

**Point at:**
- The progress stream, then the per-control table: satisfied / gap / partial, with
  a readiness percentage per control.
- The **review queue** - the pairs the verifier objected to, parked for a human
  rather than finalized. About 9% of pairs land here. "The system drafts. A person
  signs. It never signs."
- Open one row's **agent trace**: retrieve → judge → verify → finalize. "You can
  disagree with the reasoning, not just the answer."
- **Export**, and mention the same thing runs headless:
  `uv run python audit/batch.py --pack …`

## 5 - Swap the standard (1.5 min)

> "Everything you've seen was ISO 9001. Here is ISO 27001."

**Do:** change the pack picker to `iso27001.yaml`. Run two controls.

**Say:** "That second standard cost **zero Python changes** - it is a YAML file
of rubrics. The checklist is config, not code, which is why a new standard is a
domain expert's afternoon and not an engineering project."

Scores on it: 0.955 exact agreement against a 22-pair key, 0.935 on the 9001
pack the prompts were tuned against, gap recall 1.0 on both. "The second
standard cost nothing in accuracy - it actually scores a little higher than the
pack the prompts were written for. Both parks about a tenth of pairs for a
human rather than answering, and that is the right way for uncertainty to show
up."

## 6 - Swap the model endpoint (2 min)

> "Last thing, and it's the one a compliance buyer actually asks about: where
> does the data go?"

**Do, in the second terminal:**

```bash
docker compose up                                            # self-hosted
docker compose -f compose.yaml -f compose.local-llm.yaml up   # + local Ollama
```

**Point at:**
- The embedding model is **baked into the image** - the vector store builds with
  no network at all (`HF_HUB_OFFLINE=1`).
- One environment variable, `AUDITREADY_LLM_BASE_URL`, moves the LLM to vLLM,
  Ollama, or an internal gateway. `core/llm.py` is the only module that talks to
  a provider, so this was a config seam, not a rewrite.
- "For a firm whose whole reason for being audited is that they handle other
  people's data, 'it never leaves your network' is the difference between a
  pilot and a no."

---

## Closing line

> "68 labelled pairs, 0.941 agreement, gap recall 1.0 - and 0.984 over the
> pairs it actually finalizes, because it parks the rest. Answer quality is
> graded by a *different* model than the one being graded, because when a system
> grades its own homework the number comes out about 0.11 too high. Two
> standards, a live connector, and it runs air-gapped. What it does not do is
> sign anything."

## If something breaks

- **Streamlit won't start** - `.env` missing or `uv sync` not run.
- **A verdict comes back wrong** - say it. Temperature 0 is not determinism; one
  pair moved between two identical eval runs, and the honest version of that is
  more convincing than a rehearsed one.
- **The live GitHub connector is slow or rate-limited** - skip it; it is not in
  the six moments. `uv run python scripts/smoke_mcp.py` is the fast proof.
- **Nothing works at all** - fall back to the screencast (below).

## Fallback screencast

Record moments 1-6 in one take, no edits, before the review. Keep it under ten
minutes. If the live demo dies, play it and narrate over the top - the same
words, the same order.
