"""Pure report helpers shared by every surface that shows a batch report.

These used to live at the top of `audit/report_view.py`, which is a
Streamlit page. That was fine while Streamlit was the only surface. It stopped
being fine the moment a second surface (`api/`, CR-8) needed the same helpers:
importing them meant importing Streamlit into a FastAPI process, where its
`st.cache_data` decorators run against no runtime and its startup cost buys
nothing.

So the helpers moved here, and `report_view.py` re-exports them. Nothing in this
module knows what a widget or a route is; it turns a `BatchReport` into rows and
a token count into dollars, and that is all. The tests in
`tests/test_report_view.py` still address them through `report_view`, which is
the point of re-exporting - the move changed a file path, not a contract.

Req: CR-2 (the report as a product surface), CR-8.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from audit.batch import BatchReport
from core.checklists import load_pack
from core.tools import DEFAULT_PACK_PATH

CHECKLIST_DIR = Path(DEFAULT_PACK_PATH).parent
FALLBACK_PRICE_PER_M = (0.15, 0.60)  # gpt-4o-mini list price, USD per 1M tokens


def discover_packs(directory: str | Path = CHECKLIST_DIR) -> list[dict[str, Any]]:
    """Every *.yaml in the checklist dir → {label, path, pack | error}.

    Invalid packs are listed with their error instead of hidden: a typo in a
    new pack should be visible in the picker, not silently absent."""
    out = []
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            pack = load_pack(path)
            out.append({"label": f"{pack.pack.standard} · {pack.pack.id} v{pack.pack.version}",
                        "path": path, "pack": pack, "error": None})
        except (ValueError, FileNotFoundError) as exc:
            out.append({"label": f"{path.name} (invalid)", "path": path, "pack": None,
                        "error": str(exc)})
    return out


def estimate_calls(n_pairs: int) -> tuple[int, int]:
    """Judge + verify per pair, plus at most one re-judge → 2n to 3n calls."""
    return 2 * n_pairs, 3 * n_pairs


# Measured, not guessed: the committed v2 eval (audit/eval/results.json,
# 2026-09-11, gpt-4o-mini) spent 206,483 prompt and 19,590 completion tokens
# over 68 pairs - about 3,040 + 290 per pair, re-judges included. Rounded up a
# little so the budget check errs towards refusing, never towards overspending.
TYPICAL_PROMPT_TOKENS_PER_PAIR = 3_200
TYPICAL_COMPLETION_TOKENS_PER_PAIR = 320


def estimate_run_cost_usd(n_pairs: int, pricing: tuple[float, float] | None) -> tuple[float, str]:
    """What a run of `n_pairs` will roughly cost before it starts (FR-17)."""
    return estimate_cost_usd(
        n_pairs * TYPICAL_PROMPT_TOKENS_PER_PAIR, n_pairs * TYPICAL_COMPLETION_TOKENS_PER_PAIR, pricing
    )


def per_control_rows(report: BatchReport) -> list[dict[str, Any]]:
    """Per-control table rows in the pack's control order."""
    rows = []
    for cid in report.control_ids:
        pc = report.per_control[cid]
        c = pc["counts"]
        rate = pc["satisfied_rate"]
        rows.append({
            "Control": pc["title"], "Clause": pc["clause"], "Severity": pc["severity"],
            "Satisfied": c["satisfied"], "Partial": c["partial"], "Gap": c["gap"],
            "N/A": c["not_applicable"], "Review": c["needs_review"], "Error": c["error"],
            "Satisfied rate": None if rate is None else round(rate * 100),
        })
    return rows


def pair_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten per-pair results for a dataframe (one line per pair)."""
    out = []
    for r in rows:
        ver = r.get("verification") or {}
        out.append({
            "Ticket": r["ticket_id"], "Control": r["control_id"],
            "Verdict": r.get("verdict") or "-", "Status": r["status"],
            "Attempts": r.get("attempts", "-"),
            "Verifier": {True: "agrees", False: "objects", None: "-"}.get(ver.get("agrees"), "-"),
            "Citations": len(r.get("citations") or []),
        })
    return out


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int,
                      pricing: tuple[float, float] | None) -> tuple[float, str]:
    price_in, price_out = pricing or FALLBACK_PRICE_PER_M
    note = "live OpenRouter rate" if pricing else "fallback list price"
    return prompt_tokens / 1e6 * price_in + completion_tokens / 1e6 * price_out, note
