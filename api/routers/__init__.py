"""One router per concern. `api.main.create_app` includes them all.

The split is by what a route is *about*, not by HTTP verb or by layer: a reader
asking "how does chat work" opens `chat.py` and finds the request model, the
route and the error mapping together.
"""

from api.routers import audit, chat, health, packs, records, spend

ALL = (health.router, packs.router, records.router, chat.router, audit.router, spend.router)

__all__ = ["ALL", "audit", "chat", "health", "packs", "records", "spend"]
