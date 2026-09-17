"""The HTTP boundary over core/ and audit/ (CR-8).

`api/` exists so a browser can reach the engine. Streamlit calls `core/`
functions in-process; a React front end cannot, so something has to serve
JSON. That something holds no business logic — the same rule `assistant/app.py`
lives under, applied to a different transport.

    uv run uvicorn api.main:app --reload --port 8000
"""
