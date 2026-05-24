from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("PAA_RUNTIME", "vercel")

from app.main import AvatarRequestHandler, init_db, load_local_env, refresh_index

_initialized = False


def ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    load_local_env()
    init_db()
    refresh_index()
    _initialized = True


class VercelAvatarHandler(AvatarRequestHandler):
    def do_GET(self) -> None:
        ensure_initialized()
        super().do_GET()

    def do_POST(self) -> None:
        ensure_initialized()
        super().do_POST()
