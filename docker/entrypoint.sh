#!/usr/bin/env bash
# Make the container's first run a working one.
#
# The vector index is derived data, so it is not in the image (it would go
# stale against the corpus, and it belongs to the customer's evidence, not to
# us). It lives on a volume instead — which is empty the first time. Build it
# here rather than leaving the operator to press a button in the UI, and skip
# the work on every restart after that.
set -euo pipefail

# The store decides whether it is empty: a directory listing for Chroma, a
# row count for pgvector (AUDITREADY_VECTOR_STORE=pgvector, AUDITREADY_PG_DSN).
python - <<'PY'
from core.retrieval import ingest, store_is_empty, store_label

label = store_label()
if store_is_empty():
    print(f"[entrypoint] empty vector store {label} — ingesting corpus…", flush=True)
    print(f"[entrypoint] ingested {ingest()} chunks", flush=True)
else:
    print(f"[entrypoint] vector store present {label} — skipping ingest", flush=True)
PY

echo "[entrypoint] LLM endpoint: ${AUDITREADY_LLM_BASE_URL:-https://openrouter.ai/api/v1 (default)}"
exec "$@"
