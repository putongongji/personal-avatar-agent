from __future__ import annotations

from app.main import STATIC_DIR
from api._handler import VercelAvatarHandler, ensure_initialized


class handler(VercelAvatarHandler):
    def do_GET(self) -> None:
        ensure_initialized()
        try:
            self.require_admin()
            self.send_file(STATIC_DIR / "admin.html")
        except PermissionError:
            self.redirect("/login?next=/admin")
