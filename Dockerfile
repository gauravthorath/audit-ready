# AuditReady, self-hosted (CR-4).
#
# Two things shape this file. First, the image must run with no outbound
# network except the one LLM endpoint the operator names — so the embedding
# model's weights are baked in at build time rather than pulled on first
# query. Second, the LLM endpoint is a variable, not a vendor: nothing here
# mentions OpenRouter, and `AUDITREADY_LLM_BASE_URL` moves every call in the
# app (see core/llm.py).

# ---------------------------------------------------------------------------
# Stage 1 — build the environment
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies resolve from the lock alone, in their own layer, so editing
# application code does not re-download torch. `--no-install-project` is what
# keeps this layer independent of the source tree below it.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Then the project itself (core/, assistant/, audit/, api/ are packages).
COPY core ./core
COPY assistant ./assistant
COPY audit ./audit
COPY api ./api
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Bake the sentence-transformers weights (~90 MB) into the image. Without
# this, the first question in an air-gapped deployment fails on a call to
# huggingface.co — the exact call a customer running this on-premise is
# most likely to have blocked.
ENV HF_HOME=/opt/hf \
    HF_HUB_DOWNLOAD_TIMEOUT=60 \
    HF_HUB_ETAG_TIMEOUT=30
# The retry loop is not superstition: the download is a dozen separate HTTP
# requests, and a read timeout on any one of them (a 300-byte config file is
# as likely as the 90 MB weights) fails the whole build. The Hub cache is
# resumable, so an attempt after a flake picks up where the last one stopped.
RUN for attempt in 1 2 3; do \
        /app/.venv/bin/python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')" && break; \
        echo "bake attempt $attempt failed; retrying in $((attempt * 10))s"; \
        sleep $((attempt * 10)); \
    done; \
    /app/.venv/bin/python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')" \
    && find /opt/hf -name '*.onnx' -delete \
    && find /opt/hf -name '*.h5' -delete

# ---------------------------------------------------------------------------
# Stage 2 — the runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# Streamlit's health check and Chroma's sqlite backend are all this needs
# beyond the interpreter; no compiler survives into the running image.
RUN useradd --create-home --uid 10001 auditready

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    AUDITREADY_PERSIST_DIR=/app/.chroma \
    AUDITREADY_CORPUS_DIR=/app/assistant/data/corpus

WORKDIR /app

# Ownership is set by the COPY itself, never by a later `chown -R`. The
# reason is layer mechanics: chown rewrites an inode, and on overlayfs that
# means a full copy of every file it touches into a new layer — for a venv
# carrying torch that is another ~3 GB of image, and a build that dies on a
# host with less than that free.
COPY --from=builder --chown=auditready:auditready /opt/hf /opt/hf
COPY --from=builder --chown=auditready:auditready /app/.venv /app/.venv
COPY --chown=auditready:auditready core ./core
COPY --chown=auditready:auditready assistant ./assistant
COPY --chown=auditready:auditready audit ./audit
COPY --chown=auditready:auditready api ./api
COPY --chown=auditready:auditready scripts ./scripts
COPY --chown=auditready:auditready tests ./tests
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=auditready:auditready README.md pyproject.toml uv.lock ./

# The vector store is written at run time. Created here, owned here, so a
# fresh named volume inherits a writable directory instead of root's.
RUN mkdir -p /app/.chroma /app/reports \
    && chown auditready:auditready /app /app/.chroma /app/reports

USER auditready

# 8501 is the Streamlit surface (the default CMD); 8000 is the FastAPI boundary
# the React front end talks to. compose.yaml runs the same image twice, once
# per command, so one build serves both.
EXPOSE 8501 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request as u; \
u.urlopen('http://127.0.0.1:8501/_stcore/health').read()"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["streamlit", "run", "assistant/app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
