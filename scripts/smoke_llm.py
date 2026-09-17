"""Smoke test for core/llm.py — proves the OpenRouter round-trip works.

Run:  uv run python scripts/smoke_llm.py
Needs: .env with OPENROUTER_API_KEY (copy .env.example).
"""

from core.llm import LLMError, chat


def main() -> None:
    try:
        result = chat(
            [
                {"role": "system", "content": "You are a terse assistant."},
                {"role": "user", "content": "Reply with exactly the two words: smoke ok"},
            ]
        )
    except LLMError as exc:
        raise SystemExit(f"FAILED: {exc}")

    print(f"model:  {result.model}")
    print(f"reply:  {result.content!r}")
    print(f"tokens: {result.prompt_tokens} prompt + {result.completion_tokens} completion")
    print("PASSED" if "smoke ok" in result.content.lower() else "PASSED (reply differs, but round-trip works)")


if __name__ == "__main__":
    main()
