"""cs2bot.api - local dashboard API (FastAPI). See docs/ARCHITECTURE.md and docs/API.md.

Public entry points: :func:`create_app`, :func:`make_server`, :func:`run` and :class:`ApiOptions`.
"""

from cs2bot.api.main import create_app, make_server, run
from cs2bot.api.options import ApiOptions

__all__ = ["ApiOptions", "create_app", "make_server", "run"]
