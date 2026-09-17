"""Measure what an audit actually costs in time (Step 39, docs/PRODUCTION_PLAN.md).

The project has never had a latency number. Step 37 put `elapsed_ms` on every
pair and a timed `llm.call` line on every model call; this script turns that
instrumentation into the figures the deck and the site are missing, measured
the way they will be quoted.

Method, and why each part is there:

* **Warm up first.** The first pair of a cold process pays for loading the
  sentence-transformer weights and the first retrieval — 8 of the 12.4s in the
  Step 37 smoke. Quoting that as "per pair" would be dishonest. One throwaway
  pair warms the process; only the runs after it are measured.
* **Two sizes.** A 9-pair run (one ticket, every control) is the demo scale; a
  full-pack run (every ticket x every control) is the real one. Throughput at
  four workers is only visible on the big one.
* **Report percentiles, not an average.** A mean hides the re-judge tail: a
  pair the verifier objects to costs a third and fourth model call.
* **Everything is kept.** The raw report JSON, the JSON log of every model
  call, and a summary go into one dated directory beside the eval spreads, so
  a number on a slide can be traced back the way every other number can.

    uv run python scripts/measure_performance.py             # 9 pairs + full pack
    uv run python scripts/measure_performance.py --small     # 9 pairs only

Needs OPENROUTER_API_KEY (or a BYO endpoint) and an ingested vector store.
A full ISO 9001 pack run is 180 pairs, ~400 model calls, ~$0.11.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app
from api.ratelimit import RateLimits
from api.runs import RunRegistry
from audit.reporting import estimate_cost_usd
from core.llm import get_model_pricing
from core.tracing import configure_logging
from assistant.agent import DEFAULT_MODEL

OUT_ROOT = Path("marketing")


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile. Small samples, so no interpolation games."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(1, min(len(ordered), round(p / 100 * len(ordered))))
    return ordered[k - 1]


def run_audit(client: TestClient, label: str, body: dict, timeout: float = 1800) -> dict:
    started = time.perf_counter()
    r = client.post("/audit", json=body)
    if r.status_code != 202:
        raise SystemExit(f"FAILED: {label}: POST /audit -> {r.status_code} {r.text[:300]}")
    run_id = r.json()["run_id"]
    print(f"  {label}: run {run_id}, {r.json()['total_pairs']} pairs, "
          f"est. {r.json()['estimated_calls']} calls", flush=True)
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        snap = client.get(f"/audit/{run_id}").json()
        done = snap["progress"]["done"]
        if done != last:
            print(f"    {done}/{snap['progress']['total']}", end="\r", flush=True)
            last = done
        if snap["status"] in ("done", "error"):
            break
        time.sleep(1.0)
    print()
    if snap["status"] != "done":
        raise SystemExit(f"FAILED: {label} ended {snap['status']}: {snap.get('error')}")
    snap["_client_wall_s"] = round(time.perf_counter() - started, 2)
    return snap


def summarise(label: str, snap: dict, rate) -> dict:
    report = snap["report"]
    per_pair = [r.get("elapsed_ms") or 0 for r in report["rows"]]
    pairs = report["total_pairs"]
    wall_s = report["elapsed_ms"] / 1000
    usd, basis = estimate_cost_usd(report["prompt_tokens"], report["completion_tokens"], rate)
    statuses: dict[str, int] = {}
    attempts: dict[str, int] = {}
    for row in report["rows"]:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        attempts[str(row.get("attempts"))] = attempts.get(str(row.get("attempts")), 0) + 1
    return {
        "label": label,
        "pairs": pairs,
        "wall_s": round(wall_s, 2),
        "client_wall_s": snap["_client_wall_s"],
        "pairs_per_minute": round(pairs / wall_s * 60, 1) if wall_s else None,
        "pair_ms": {
            "p50": round(percentile(per_pair, 50)),
            "p95": round(percentile(per_pair, 95)),
            "min": min(per_pair) if per_pair else 0,
            "max": max(per_pair) if per_pair else 0,
            "mean": round(statistics.mean(per_pair)) if per_pair else 0,
        },
        "tokens": {
            "prompt": report["prompt_tokens"],
            "completion": report["completion_tokens"],
            "prompt_per_pair": round(report["prompt_tokens"] / pairs),
            "completion_per_pair": round(report["completion_tokens"] / pairs),
        },
        "usd": round(usd, 4),
        "usd_per_pair": round(usd / pairs, 6),
        "cost_basis": basis,
        "readiness_score": report["readiness_score"],
        "statuses": statuses,
        "attempts": attempts,
    }


def call_latencies(log_text: str) -> dict:
    calls = []
    for line in log_text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("event") == "llm.call" and entry.get("outcome") == "ok":
            calls.append(entry["elapsed_ms"])
    if not calls:
        return {"calls": 0}
    return {
        "calls": len(calls),
        "p50_ms": round(percentile(calls, 50)),
        "p95_ms": round(percentile(calls, 95)),
        "max_ms": max(calls),
        "mean_ms": round(statistics.mean(calls)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--small", action="store_true", help="9-pair run only (skip the full pack)")
    ap.add_argument("--pack", default="iso9001-sample")
    args = ap.parse_args(argv)

    out = OUT_ROOT / f"perf-{date.today().isoformat()}"
    out.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    configure_logging(fmt="json", level="INFO", stream=buf)

    app.state.runs = RunRegistry(out / "reports")
    app.state.limits = RateLimits.build(0, 0, 0)  # measuring, not defending

    rate = get_model_pricing(DEFAULT_MODEL)
    results = []
    with TestClient(app) as client:
        print(f"model {DEFAULT_MODEL}; pricing {rate}; store {client.get('/health').json()['vector_store']}")

        print("warm-up (1 pair, discarded — weights and first retrieval)", flush=True)
        run_audit(client, "warm-up", {"pack_id": args.pack, "ticket_ids": ["TCK-1001"],
                                      "control_ids": ["ISO9001-8.6-release-signoff"]})
        warm_mark = len(buf.getvalue())

        print("measured: one record x every control", flush=True)
        small = run_audit(client, "9 pairs", {"pack_id": args.pack, "ticket_ids": ["TCK-1002"]})
        results.append(summarise("one record x every control", small, rate))
        small_mark = len(buf.getvalue())
        (out / "report-small.json").write_text(json.dumps(small["report"], indent=1))

        if not args.small:
            print("measured: every record x every control", flush=True)
            full = run_audit(client, "full pack", {"pack_id": args.pack})
            results.append(summarise("every record x every control", full, rate))
            (out / "report-full.json").write_text(json.dumps(full["report"], indent=1))

    log_text = buf.getvalue()
    (out / "run.jsonl").write_text(log_text)
    results[0]["llm_calls"] = call_latencies(log_text[warm_mark:small_mark])
    if len(results) > 1:
        results[1]["llm_calls"] = call_latencies(log_text[small_mark:])

    summary = {
        "measured_at": date.today().isoformat(),
        "model": DEFAULT_MODEL,
        "pricing_source": "openrouter" if rate else "fallback list price",
        "workers": 4,
        "warm": True,
        "runs": results,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1))

    print("\n" + json.dumps(summary, indent=1))
    print(f"\nwritten: {out}")
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
