"""LLM client — the ONLY module in the project that talks to an LLM provider (NFR-1).

Everything else calls `chat()` and stays provider-ignorant. Swapping OpenRouter
for a customer's own endpoint (CR-4) means changing env vars, not code:
OpenRouter speaks the OpenAI wire protocol, so we use the official `openai` SDK
with a custom `base_url`. `AUDITREADY_LLM_BASE_URL` and `AUDITREADY_LLM_API_KEY`
are that switch, and `assistant/agent.py` resolves its LangChain handle through
the same two functions so one variable moves every call in the app, not some.

Design notes
------------
- Returns token usage with every reply so the UI can display cost (FR-17).
- Raises a single exception type, `LLMError`, so callers can fail gracefully
  without knowing about the zoo of SDK exceptions underneath (FR-15).
- Retries transient failures (rate limits, timeouts) with exponential backoff;
  never retries auth or bad-request errors — those won't fix themselves.
- Framework-agnostic: no LangChain imports here (FR-7). LangChain will get its
  own handle to OpenRouter in assistant/agent.py; this module serves the plain
  tools and the eval harness.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

import httpx  # already a transitive dependency of the openai SDK
from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from core.tracing import elapsed_ms, log_event

load_dotenv()  # reads .env from the project root; real env vars take precedence

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = os.getenv("AUDITREADY_MODEL", "openai/gpt-4o-mini")

# A key placeholder for endpoints that authenticate by network position rather
# than by token (a vLLM or Ollama container on the compose network). The SDK
# insists on a non-empty api_key, so something has to go in the field; this
# name makes it obvious in a traceback that no real credential was configured.
UNAUTHENTICATED_PLACEHOLDER = "not-needed"

# Grader model for the eval harness's LLM-as-judge metrics (FR-18, CR-7). NOT
# core/judge.py's verdict model — that is DEFAULT_MODEL like everything else.
# Defaults to a different family from the system model on purpose: a model
# grading its own answers marks them generously, which is the self-grading
# bias the v1 numbers carry. Overridable per run, so the grader can be
# swapped without touching the harness (or a customer's BYO endpoint, CR-4).
DEFAULT_JUDGE_MODEL = os.getenv("AUDITREADY_JUDGE_MODEL", "google/gemini-2.5-flash")

def llm_base_url() -> str:
    """Where chat completions go. One env var, every call in the app (CR-4).

    Self-hosted deployment means the customer's data must not leave their
    boundary, and the only thing in this project that leaves it is the judging
    call. Because OpenRouter speaks the OpenAI wire protocol, so do vLLM,
    Ollama, LM Studio, Azure OpenAI and llama.cpp: pointing at any of them is
    a base-URL change, which is why this is config and not a provider
    abstraction. Read per call rather than frozen at import so a test (or a
    demo) can move the endpoint without reloading the module.
    """
    return os.getenv("AUDITREADY_LLM_BASE_URL", OPENROUTER_BASE_URL).rstrip("/")


def llm_api_key() -> str:
    """The credential for that endpoint, or the placeholder if it needs none.

    Precedence: `AUDITREADY_LLM_API_KEY` (the endpoint-neutral name a
    self-hosting customer sets) then `OPENROUTER_API_KEY` (what a developer
    already has in `.env`). A missing key is only an error on OpenRouter
    itself; a local endpoint is reached over the container network and
    usually has no notion of a token, so failing there would block the
    deployment story this seam exists for.
    """
    key = os.getenv("AUDITREADY_LLM_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    if key:
        return key
    if llm_base_url() != OPENROUTER_BASE_URL:
        return UNAUTHENTICATED_PLACEHOLDER
    raise LLMError(
        "No LLM credential is set. Copy .env.example to .env and add "
        "OPENROUTER_API_KEY from https://openrouter.ai/keys — or point "
        "AUDITREADY_LLM_BASE_URL at your own OpenAI-compatible endpoint."
    )


_RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError)
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0


class LLMError(Exception):
    """Single failure type the rest of the app handles. Message is user-safe."""


# One `llm.call` line per attempt, with the model, the latency, the tokens
# and the outcome. This is the AI leg of the trace: the correlation id bound by
# the API (or the batch runner) is appended by the formatter, so a slow answer
# can be followed from the browser to the exact call that was slow.
log = logging.getLogger("auditready.llm")


@dataclass
class ChatResult:
    """One completed LLM exchange, with the accounting the UI needs."""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class _ClientHolder:
    """Lazy singleton so importing this module never requires an API key.

    Lock-guarded because coverage_report calls chat() from a thread pool:
    without it, concurrent first calls could each build a client. The
    OpenAI client itself is thread-safe once constructed."""

    client: OpenAI | None = field(default=None)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self) -> OpenAI:
        with self._lock:
            if self.client is None:
                self.client = OpenAI(
                    base_url=llm_base_url(), api_key=llm_api_key(), timeout=60.0
                )
            return self.client

    def reset(self) -> None:
        """Drop the cached client so the next call re-reads the endpoint.

        The holder caches for the life of the process, which is right for a
        Streamlit session and wrong for a test that moves the env, or for a
        demo that swaps endpoints without a restart."""
        with self._lock:
            self.client = None


_holder = _ClientHolder()


def reset_client() -> None:
    """Forget the cached client; the next `chat()` rebuilds it from the env."""
    _holder.reset()


def chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> ChatResult:
    """Send a chat completion request and return the reply plus token usage.

    `messages` uses the standard role/content shape:
        [{"role": "system", "content": ...}, {"role": "user", "content": ...}]

    Temperature defaults to 0: compliance verdicts should be as deterministic
    as the model allows — creativity is a bug here, not a feature.
    """
    model = model or DEFAULT_MODEL
    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        started = time.perf_counter()
        try:
            response = _holder.get().chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
            )
            choice = response.choices[0]
            usage = response.usage
            result = ChatResult(
                content=choice.message.content or "",
                model=response.model or model,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
            )
            log_event(log, "llm.call", model=result.model, attempt=attempt + 1,
                      elapsed_ms=elapsed_ms(started), prompt_tokens=result.prompt_tokens,
                      completion_tokens=result.completion_tokens, outcome="ok")
            return result
        except _RETRYABLE as exc:
            last_error = exc
            log_event(log, "llm.call", logging.WARNING, model=model, attempt=attempt + 1,
                      elapsed_ms=elapsed_ms(started), outcome="retry",
                      error=type(exc).__name__)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
        except APIStatusError as exc:
            # 4xx-style errors (bad key, bad model name…): retrying won't help.
            log_event(log, "llm.call", logging.ERROR, model=model, attempt=attempt + 1,
                      elapsed_ms=elapsed_ms(started), outcome="rejected",
                      status=exc.status_code)
            raise LLMError(f"LLM request rejected ({exc.status_code}): {exc.message}") from exc

    raise LLMError(f"LLM unavailable after {_MAX_RETRIES} attempts: {last_error}") from last_error


# ---------------------------------------------------------------------------
# Model pricing (FR-17 support)
#
# OpenRouter publishes every model's rates on a public, keyless endpoint —
# so the UI's cost display can use the ACTUAL price of whatever model
# AUDITREADY_MODEL selects, instead of a hardcoded sheet that silently goes
# stale (or worse, stays priced for gpt-4o-mini after someone switches to a
# 30x-dearer model).
# ---------------------------------------------------------------------------


def _pricing_from_payload(payload: dict, model: str) -> tuple[float, float] | None:
    """Extract (input, output) USD per 1M tokens for one model from the
    /models response. Pure and unit-testable; the payload is untrusted
    external data, so every missing/malformed shape returns None rather
    than raising — pricing is a display nicety, never worth crashing for.

    OpenRouter quotes prices as USD-per-single-token strings (e.g.
    "0.00000015"); we scale to per-million, the unit humans reason in.
    """
    for entry in payload.get("data", []):
        if isinstance(entry, dict) and entry.get("id") == model:
            pricing = entry.get("pricing")
            if not isinstance(pricing, dict):
                return None
            try:
                return (
                    float(pricing["prompt"]) * 1_000_000,
                    float(pricing["completion"]) * 1_000_000,
                )
            except (KeyError, TypeError, ValueError):
                return None
    return None


def get_model_pricing(model: str | None = None) -> tuple[float, float] | None:
    """Fetch the live (input, output) USD-per-1M-token rates for a model.

    Returns None on any failure (offline, timeout, unknown model) — callers
    fall back to their own estimate. Deliberately uncached here: the caller
    owns the caching policy (the Streamlit app caches for an hour), and a
    stateless function stays trivially testable.
    """
    model = model or DEFAULT_MODEL
    if llm_base_url() != OPENROUTER_BASE_URL:
        return None  # a BYO endpoint (CR-4) has no price list to read, and a
        # self-hosted model's cost is the customer's hardware, not per token
    try:
        response = httpx.get(f"{OPENROUTER_BASE_URL}/models", timeout=5.0)
        response.raise_for_status()
        return _pricing_from_payload(response.json(), model)
    except Exception:
        return None
