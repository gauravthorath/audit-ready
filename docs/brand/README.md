# Brand assets

One mark, used everywhere: the web app (`web/components/logo.tsx`, `web/app/icon.svg`,
`web/app/apple-icon.png`), the Streamlit fallback (`st.logo`), the marketing site
(inlined), and the deck.

- `auditready-mark.svg` — the square mark. Source of truth; edit this, then re-render.
- `auditready-mark.png` — 512px render for places that need a bitmap (Streamlit).
- `auditready-wordmark.svg` — mark + name, for headers and social cards.

The shape: a check standing on a rule — a verdict resting on its citation.
Brand green `#0F6E56` (the same `--color-green` as the app); the mark is a filled
square so it survives 16px favicons and both themes unchanged.

Re-render the PNGs with `uv run python docs/brand/render_mark.py` (Pillow, already
a dependency through Streamlit). `qlmanage` ignores the viewBox and renders SVGs at
their intrinsic size, so it is not used.
