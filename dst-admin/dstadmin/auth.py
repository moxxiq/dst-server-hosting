"""auth.py — HTTP Basic on every request. Empty ADMIN_PASSWORD fails closed with 500."""
import base64
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, user: str):
        super().__init__(app)
        self.user = user.encode()

    async def dispatch(self, request, call_next):
        password = os.environ.get("ADMIN_PASSWORD", "").encode()
        if not password:
            return PlainTextResponse("ADMIN_PASSWORD is not set", status_code=500)
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).partition(b":")
            except ValueError:
                user = pwd = b""
            ok = secrets.compare_digest(user, self.user) and secrets.compare_digest(pwd, password)
        if not ok:
            return PlainTextResponse(
                "authentication required", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="dst-admin"'},
            )
        return await call_next(request)
