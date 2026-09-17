"""The committed OpenAPI document must match the app (CR-8).

`web/lib/schema.d.ts` is generated from `api/openapi.json`. If a response model
changes and the JSON is not re-exported, the TypeScript types silently describe
an API that no longer exists. This test is the seam: it fails in CI the moment
the two disagree, and the message says which command fixes it.
"""

from __future__ import annotations

import json
from pathlib import Path

from api.main import app

DOC = Path(__file__).resolve().parents[1] / "api" / "openapi.json"


def test_committed_openapi_matches_the_app():
    live = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    assert DOC.exists(), "api/openapi.json missing — run: uv run python scripts/export_openapi.py"
    assert DOC.read_text(encoding="utf-8") == live, (
        "api/openapi.json is stale — run: uv run python scripts/export_openapi.py "
        "&& (cd web && pnpm gen:types)"
    )


def test_every_json_route_is_typed():
    """No route may fall back to a bare object: that is how the drift began."""
    spec = app.openapi()
    untyped = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            responses = op.get("responses", {})
            ok = responses.get("200") or responses.get("202") or {}
            content = ok.get("content", {})
            if "application/json" not in content:
                continue  # text/plain markdown, SSE
            schema = content["application/json"].get("schema", {})
            typed = "$ref" in schema or "$ref" in schema.get("items", {})
            if not typed:
                untyped.append(f"{method.upper()} {path}")
    assert untyped == [], f"routes without a response model: {untyped}"
