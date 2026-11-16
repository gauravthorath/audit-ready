"""Write the API's OpenAPI document to api/openapi.json (CR-8).

The committed file is what `web/` generates its TypeScript types from, and
`tests/test_openapi_contract.py` fails when it is stale - so a change to a
response model is a three-step edit (model, this script, `pnpm gen:types`) and
a forgotten step is a red test, not a runtime surprise in a browser.

    uv run python scripts/export_openapi.py          # write
    uv run python scripts/export_openapi.py --check  # exit 1 if stale
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "api" / "openapi.json"


def render() -> str:
    from api.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    text = render()
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"STALE: {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT} "
                  "does not match the running app - run: uv run python scripts/export_openapi.py")
            return 1
        print("openapi.json is current")
        return 0
    OUT.write_text(text, encoding="utf-8")
    n = len(json.loads(text).get("components", {}).get("schemas", {}))
    print(f"wrote {OUT} ({n} schemas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
